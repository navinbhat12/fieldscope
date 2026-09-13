# Design

How Fieldscope is built and why. Each decision below records the constraint
that forced it, the alternatives considered, and — where it has been run — the
measured consequence. Decisions still open are marked as such rather than
quietly assumed.

---

## 1. Problem

Given an arbitrary field boundary, return everything known about the ground
underneath it: soil composition, land cover, and drought severity.

Three public datasets describe that same land, and all three disagree about how
to divide it:

| Layer | Geometry | Granularity |
|---|---|---|
| Soil survey (SSURGO) | irregular polygons | median 3.37 acres |
| Land cover (CDL) | uniform raster grid | 30 m pixels |
| Drought severity (USDM) | a handful of huge multipolygons | national, ~10⁴ km² regions |

Answering the question means reconciling all three geometrically. Geometry is
expensive, and the naive formulation is worse than it looks: at county scale a
brute-force point-in-polygon comparison is ~10¹¹ tests.

The hard constraint is that this has to be answerable *interactively*. A user
draws a field and expects an answer immediately, which rules out doing the
overlay per request.

## 2. Goals and non-goals

**Goals**

- Real public data end to end. No synthetic records anywhere in the pipeline.
- A correctness story: results checked against an independently computed
  ground truth, not just eyeballed.
- Clean separation between offline computation and the online serving path.
- Measured numbers for every performance claim.

**Non-goals**

- Streaming ingestion. Inputs change weekly at most; batch is the honest fit.
- National coverage. Scope is Indiana, deliberately (§3).
- Per-request freshness. Staleness bounded by the refresh cadence is acceptable
  and is a direct consequence of §5.1.

## 3. Scale

Two scopes. The pipeline is developed against a single county for fast
iteration and run unchanged across the state.

| | Tippecanoe County | Indiana |
|---|---|---|
| Land cover records | 2,079,440 *(measured)* | 104,126,688 *(measured)* |
| Soil polygons | 30,264 *(measured)* | 1,341,119 *(measured)* |
| Soil geometry on disk | 36.6 MB *(measured)* | ~1.6 GB *(projected)* |
| Acquisition WFS tiles | 4 | 238 |

Indiana land cover was counted directly from the state raster (non-zero pixels
of a 9143×15717 grid). The polygon count came from a Soil Data Access
aggregate query, not an extrapolation — an earlier estimate of ~800k was low
by 67%, which materially changes §5.5.

## 4. Architecture

```
OFFLINE (batch, scheduled)                ONLINE (always on, edge)

 public datasets                           Workers API
   CDL raster ──┐                            key lookups only
   SSURGO      ─┼──▶ Spark + Sedona          KV cache for hot keys
   USDM        ─┘      spatial join                 │
                            │                       ▼
                            ▼                   map frontend
                  compact lookup table   ──load──▶
```

The split in §5.1 is the load-bearing decision; everything else follows from
it.

---

## 5. Decisions

### 5.1 Precompute the overlay offline; serve only lookups

**Status:** decided, batch half built

**Context.** The overlay is expensive geometry and the API must be
interactive.

**Options.**

1. Compute per request against indexed source geometry (PostGIS + GiST).
   Always fresh, no storage amplification, but every request pays the geometry
   cost and the API needs a spatially-capable database on the hot path.
2. Precompute the overlay for all land once, offline, and reduce it to a
   lookup table the serving tier can answer from without geometry.

**Decision.** Option 2.

**Why.** Precomputation wins when the query space is bounded and the inputs
change more slowly than they are queried. Both hold here: the land area is
fixed, and soil changes effectively never, land cover annually, drought weekly.

**Consequences.**

- The serving tier needs no spatial capability at all, which is what makes a
  plain key-value or SQLite-class store viable at the edge (see §5.7).
- Staleness is bounded by refresh cadence, and drought — the only fast-moving
  input — needs a refresh path (cheap, by §5.4).
- Measured compression at county scale: 2,079,440 input records → **5,349
  output rows**, 389:1. This ratio is the entire argument for the
  architecture, expressed as one number.

### 5.2 Run the join in an equal-area projection (EPSG:5070)

**Status:** decided, implemented

**Context.** The output is measured in acres. Degrees are not a unit of area,
and area error in lat/lon varies with latitude.

**Decision.** Albers Equal Area (NAD83 / Conus Albers). The CDL raster is
already native to it, so the largest table in the job needs no reprojection —
only the two much smaller vector layers are transformed.

**Consequence / open issue.** Serving-side grid cells must be computable
inside a Worker from lon/lat. Either the grid is defined in lat/lon directly,
or the Worker performs an Albers transform in JS. This feeds §5.6.

### 5.3 Build the spatial index over the small side

**Status:** decided, implemented, measured

**Context.** Left to itself, Sedona built its R-tree over the 2.08M-row point
side and broadcast that — a single-threaded index build over the largest table
in the job.

**Decision.** Force the index onto the 30,264-row polygon side so the large
side streams through it in parallel.

**Discussion.** The planner has no cardinality estimate for a spatial
predicate, so it cannot make this choice itself. The general rule is to index
whichever side is cheaper to build over and probe more times — here the
asymmetry is ~69:1.

**Consequence.** This strategy is viable *only* while the indexed side fits in
memory and can be broadcast to every executor. It explicitly does not survive
the state scale-up; see §5.5.

### 5.4 Attach drought at polygon granularity, not per pixel

**Status:** decided, implemented

**Context.** The drought layer is 5 rows, but they are national multipolygons
totalling 1,332 parts and 178k vertices. Tested against every pixel they
dominated total runtime.

**Decision.** Clip drought to the area of interest, join it to soil polygons,
and let pixels inherit severity from the polygon containing them. A polygon
straddling a drought boundary takes the more severe class.

**Error bound.** Drought regions are on the order of 10⁴ km²; the median soil
polygon is 0.0136 km². The approximation is six orders of magnitude below the
resolution of the layer being approximated. Taking the worse class on a
straddle is the conservative direction for a risk overlay.

**Consequence.** This also makes the weekly refresh cheap: only the
polygon→drought mapping has to be recomputed, not the full per-pixel join.
An approximation made for performance turned out to define the incremental
update path.

### 5.5 Join strategy at state scale — OPEN

**Status:** open; next milestone

**Context.** Indiana has 1,341,119 soil polygons against 104,126,688 land
cover records, projecting to roughly 1.6 GB of geometry. The broadcast
strategy of §5.3 cannot survive this — it will exceed the broadcast threshold
or exhaust the driver.

**Options.**

1. **Spatially-partitioned join.** Partition both sides onto a shared grid
   (KDB-tree or quadtree), join each partition locally, no broadcast.
2. **Partition by county, union the results.** Trivially parallel, but it is
   really 92 independent jobs rather than one distributed one, and polygons
   crossing county lines need deduplication.

**Leaning.** Option 1. Option 2 sidesteps the actual problem rather than
solving it.

**Mechanism.** Sedona ships both strategies as separate physical operators —
`BroadcastIndexJoinExec`, which §5.3 currently forces, and `RangeJoinExec`,
which spatially partitions both sides and joins each partition locally. So the
work is selecting and tuning the second, not implementing it. The knobs that
matter:

| Setting | Role |
|---|---|
| `autoBroadcastJoinThreshold` | decides broadcast vs. partitioned; Indiana crosses it |
| `joinGridType` | the partitioner: `KDBTREE`, `QUADTREE`, `EQUALGRID`, `ZORDER`, `QUADTREE_RTREE` |
| `joinSpartitionDominantSide` | which side's distribution drives partition boundaries (`LEFT`/`RIGHT`/`NONE`) |
| `fallbackPartitionNum` | partition count, trading parallelism against shuffle |
| `useIndex` / `indexType` | whether each partition builds a local index, and of what type |

`joinGridType` and `joinSpartitionDominantSide` are the two that address skew
directly: `EQUALGRID` is the uniform grid that skew defeats, while `KDBTREE`
and `QUADTREE` subdivide by actual data density, and the dominant side chooses
whose density they follow.

**Discussion points.**

- Skew is the real risk: soil polygon density tracks survey detail and land
  use, so a uniform grid will produce badly uneven partitions.
- Partition count trades parallelism against shuffle cost; both sides must be
  shuffled, unlike the broadcast case where only one moves.
- The naive extrapolation (104M ÷ observed county throughput) is meaningless,
  because it assumes a join strategy that will not run at this size.

### 5.6 Serving key design — OPEN

**Status:** open; blocks §5.7

**Context.** The current output is keyed by
`(mukey, musym, areasymbol, crop_code, drought_class)`. That answers "what is
on this soil map unit" — but the product question is "what is under the field
I just drew," which is not the same lookup.

**Options.**

1. **Spatial query at request time** against soil polygons with a GiST index.
   Straightforward and exact, but reintroduces geometry into the hot path and
   undermines §5.1.
2. **Grid-cell precomputation.** Bucket the output into fixed cells and store
   cell → overlay summary. A drawn field maps to covering cells by arithmetic,
   with no geometry at request time.

**Leaning.** Option 2, consistent with §5.1.

**Discussion points.**

- Cell size is the central knob: finer cells mean better spatial fidelity and
  more rows. Row count ≈ cells × distinct (soil, cover) combinations per cell,
  so a per-combination schema risks multiplying into millions of rows at state
  scale. Storing one row per cell with the summary as a compact blob keeps the
  table small and the lookup single-keyed.
- The cell function must be computable in a Worker from lon/lat (see §5.2).
- This is an accuracy-for-latency trade and needs a stated error budget, not
  just a chosen number.

### 5.7 Serving store — OPEN

**Status:** open; decide once §5.6 fixes the row count

**Context.** Deliberately deferred until the output size was known. County
output is 5,349 rows, but §5.6 changes the shape, so the county figure does
not settle it.

**Note.** Because §5.1 removed request-time geometry, the store does not need
spatial support — which is precisely what puts a SQLite-class edge database in
play alongside PostGIS. The architectural choice cascades into the
infrastructure choice.

### 5.8 State-scale acquisition — OPEN

**Status:** open

**Context.** SSURGO geometry is only reliably available via WFS in bounding-box
tiles. Indiana's bbox is 238 tiles at 0.25°; observed county tiles took 16–82s
each, implying a multi-hour job against a government service with no
availability guarantee.

**Requirements this implies.** Per-tile caching so a failure costs one tile
rather than the run; idempotent re-invocation; retry with backoff; and
deduplication across tile seams — already handled at county scale, where
30,671 fetched features deduplicated to 30,264.

### 5.9 An honest drought layer in the demo — OPEN

**Status:** open; blocks the frontend milestone

**Context.** The pipeline correctly reports that Indiana has essentially no
drought. Tippecanoe has none at all, and statewide the worst class present is
D0 ("abnormally dry"). The layer is therefore truthful and empty, which makes
for a demo that appears broken.

**Options.**

1. Synthesize drought values so the layer renders. Rejected — it would put
   fabricated data in a pipeline whose entire premise is real public data.
2. Also load a historical week from the USDM archive, which goes back to 1999,
   and let the UI select between weeks. 2012 was a severe drought year in
   Indiana. The archive uses the same URL pattern as the current release, so
   the acquisition script barely changes.

**Leaning.** Option 2. The historical week is real data, and labelling it as a
specific past week is honest in a way that inventing values is not.

**Consequence.** The serving store must key on the drought week, not assume a
single current one — which is worth knowing before §5.6 fixes the key schema,
not after.

---

## 6. Correctness

`scripts/validate_join.py` recomputes the same answer by an unrelated route —
masking the raster with rasterio and counting in NumPy on a single machine —
then diffs it against the distributed result.

Current status: five of six sampled soil map units match exactly; the sixth
differs by one pixel in 151,503, where a polygon edge crosses a pixel centre.
Both the agreement and that single-pixel artifact reproduce exactly across a
full environment rebuild, so the pipeline is deterministic.

An independent cross-check: soil polygons cover 90.3% of the raster footprint,
and 90.21% of land cover records match a soil unit. Those two figures are
computed by different methods and agree to within 0.1%, which is why the
unmatched 10% is understood as open water and unsurveyed land rather than data
loss.

## 7. Benchmarking method

Timing on a local multi-core session varies widely with JVM warmup and page
cache state — observed spread on identical code and data was 14.6s to 54.3s.
Single-run figures are therefore not reported. `scripts/benchmark.py` runs N
iterations and reports the median and range, and any performance claim in this
repository cites that, the sample size, and the hardware.

## 8. Milestones

| # | Milestone | Status |
|---|---|---|
| 1 | Reproducible acquisition of four public datasets | done |
| 2 | Distributed join, validated against single-machine truth | done |
| 3 | State-scale join strategy (§5.5) + Indiana run | next |
| 4 | Serving key design (§5.6) and store (§5.7) | open |
| 5 | Edge API + measured multi-region latency | open |
| 6 | Map frontend, public demo — needs §5.9 first | open |
| 7 | Scheduled weekly drought refresh (cheap, by §5.4) | stretch |

Milestone 3 is the load-bearing one: it is where the broadcast strategy of §5.3
stops working and the join has to become genuinely distributed. It also settles
the output size that §5.6 and §5.7 depend on, which is why it comes before the
serving work rather than after it.
