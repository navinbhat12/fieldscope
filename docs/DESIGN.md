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

All figures below are measured.

| | Tippecanoe County | Indiana |
|---|---|---|
| Land cover records | 2,079,440 | 104,126,688 |
| Soil polygons (in scope) | 30,264 | 1,341,119 |
| Soil polygons (as downloaded) | 30,264 | 1,482,366 |
| Distinct soil map units | 429 | 7,245 |
| Soil geometry on disk | 36.6 MB | 2.12 GB |
| Acquisition WFS tiles | 4 | 196 of 238 |
| Acquisition wall time | 23s | 1339s (22.3 min) |

Indiana land cover was counted directly from the state raster (non-zero pixels
of a 9143×15717 grid). An early estimate of ~800k soil polygons was low by
67%, which materially changes §5.5.

The two polygon counts differ because border tiles return soil from Illinois,
Ohio, Kentucky and Michigan: 141,247 polygons, 9.5% of the download, that no
Indiana land cover record can ever match. See §5.10.

**An independent check on acquisition completeness.** The 1,341,119 figure was
obtained twice by unrelated routes that agree exactly, to the polygon: once
from a Soil Data Access SQL aggregate before any download began, and once by
counting `areasymbol LIKE 'IN%'` in the assembled output of 196 WFS tiles. A
gap in tile coverage would undercount and a deduplication error would shift
the total in one direction or the other; neither happened, and all 92 county
survey areas are present.

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

### 5.5 Join strategy at state scale — grid chunking

**Status:** decided and implemented; verified on county data, not yet run at
state scale (see §9)

**Context.** Indiana has 1,341,119 soil polygons against 104,126,688 land
cover records, in 2.12 GB of geometry. The broadcast strategy of §5.3 cannot
survive this.

**Measured, not assumed.** `scripts/broadcast_limit.py` holds the point side
fixed at 2,000,000 and scales only the polygon side, so the single variable is
the size of the thing being broadcast. Each rung runs in its own subprocess,
because a JVM that has thrown `OutOfMemoryError` cannot be trusted to report
the next rung honestly.

| Polygons | Outcome | Join time |
|---|---|---|
| 30,000 | ok | 6.2s |
| 100,000 | ok | 11.1s |
| 300,000 | ok | 37.5s |
| 600,000 | **`OutOfMemoryError: Java heap space`** | — |

So broadcast stops working between 300,000 and 600,000 polygons at an 8 GB
driver heap, and Indiana needs 1,341,119 — past the limit by at least 2.2×.
Note the time column as well: 3× the polygons from 100k to 300k cost 3.4× the
time, so the strategy was already scaling badly before it stopped scaling at
all.

The failure is heap exhaustion while assembling the broadcast, not Spark
refusing an over-large broadcast relation. That distinction matters because
the two have different remedies — a bigger driver would move this threshold,
and no driver size reaches 1.34M polygons on a 17 GB machine.

**Options.**

1. **Sedona's spatially-partitioned join** (`RangeJoinExec`). Partition both
   sides onto a shared KDB-tree grid, join each partition locally, no
   broadcast. Selected by setting `sedona.join.autoBroadcastJoinThreshold=-1`
   and `sedona.join.gridtype=kdbtree`.
2. **Grid chunking.** Cut the area into blocks and run an independent
   broadcast join per block, so every block stays under the §5.3 ceiling.

**Option 1 was tried first, and rejected on measurement.**

It is *correct*: on county data it reproduced the broadcast result exactly —
90,358 matched, 90.19%, 2,948 rows — which is the check that matters, since a
join strategy that returns different answers is not a strategy. But it is far
too slow here:

| Points (Indiana, 1.48M polygons) | Result |
|---|---|
| 50,000 | 410.7s — 121 records/sec |
| 500,000 | unfinished at a 900s timeout |
| county, 100k pts / 30k polys | 151s vs 3s for broadcast — **50× slower** |

The marginal rate works out near 900 records/sec, which puts Indiana's 104M
records past **thirty hours**. Two things were visibly wrong: 2 GB of geometry
is shuffled across the network and re-indexed per partition, and the KDB-tree
partitioner did not balance the load — the Spark UI showed 15 of 16 tasks
finished and idle while a single straggler held the stage open, which is
exactly the skew this partitioner is supposed to prevent.

**Decision: option 2.** Each block holds few enough polygons to broadcast, so
every block takes the fast path and *no geometry is shuffled at all*.

**Why a regular grid and not counties.** County bounding boxes overlap. A
point inside two of them would be joined twice and counted twice, silently
inflating every acreage in the output — a wrong answer that still looks
plausible. A grid partitions the plane exactly: a point's block is arithmetic
on its coordinates, so double counting is impossible by construction. Polygons
straddling a block boundary are sent to both blocks, which is harmless, since
a polygon only ever matches points already inside that block.

**Measured** (Tippecanoe, 3×3 grid, 6 cores):

| | Broadcast | Sedona partitioned | Grid chunked |
|---|---|---|---|
| Matched | 1,875,956 | 1,875,956 | **1,875,956** |
| Match rate | 90.21% | 90.21% | **90.21%** |
| Output rows | 5,349 | 5,349 | **5,349** |
| Throughput | ~100k rec/s | ~900 rec/s | **93,706 rec/s** |

All three agree exactly, and `validate_join.py` passes against the
single-machine ground truth on the chunked output, including the known
one-pixel artifact on mukey 164315.

**Discussion points.**

- The generic tool was the wrong tool, and only measurement showed it. Sedona's
  partitioner is general-purpose; the grid exploits something known about this
  problem — that land cover records are already uniformly distributed over a
  raster — which a general partitioner cannot assume.
- Correctness came before performance: the chunked path was verified against a
  known-good answer on county data before being pointed at the state.
- Overlapping partitions are a correctness bug, not a performance one, which is
  why counties were rejected as the chunk unit despite being the obvious choice.

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

### 5.8 State-scale acquisition

**Status:** decided, implemented, run

**Context.** SSURGO geometry is only reliably available via WFS in bounding-box
tiles. Indiana's bounding box is 238 tiles at 0.25°, against 4 for a county,
and the first sequential estimate put it beyond an hour as a single
uninterruptible request stream against a government service with no
availability guarantee.

**Decision.** Three changes, in order of effect:

1. **Skip tiles that miss the area's real outline.** The bounding box is a
   rectangle and Indiana is not: 42 of 238 tiles fall entirely in neighbouring
   states and are never requested.
2. **Fetch through a four-worker pool.** The bound is politeness toward a
   public federal service, not client throughput.
3. **Cache each tile to disk as it lands,** keyed by position in the full
   bounding box so the cache stays valid across differently-scoped runs. This
   is what makes a twenty-minute download safe to interrupt: a run that dies
   at tile 180 resumes at 180.

**Measured.** County output unchanged (30,671 → 30,264 polygons, 429 map
units, identical bounds) at 23s rather than 82s. Indiana: 196 tiles in 1339s
(22.3 min) at 7.2s a tile, 1,522,662 polygons deduplicating to 1,482,366,
2.12 GB.

**On estimating this.** A ten-tile pilot, sampled at a stride across the state
rather than as a contiguous block, projected 19 minutes against the 22.3
actually taken — close enough to have been worth the three minutes it cost.
The earlier "over an hour" figure came from extrapolating four tiles of one
unusually polygon-dense farmland county, which is the same species of
unmeasured extrapolation that had already put a wrong throughput number in the
README. Pilot, then commit.

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

### 5.10 Out-of-state polygons in the state download — keep them

**Status:** decided against filtering, on measurement that contradicted the
reasoning

**Context.** WFS is queried by bounding box, and Indiana's bounding box
overlaps four other states. 141,247 of the 1,482,366 polygons downloaded —
9.5% — belong to Illinois, Ohio, Kentucky or Michigan survey areas.

**Why it matters more than 9.5% sounds.** Under the broadcast strategy of §5.3
this would be rounding error. Under the partitioned join of §5.5 every polygon
is shuffled across the network and indexed within its partition, so the
fraction is paid in shuffle volume and index build time, not just memory.

**Options.**

1. Keep them. No filter step, and the pipeline stays correct if the AOI ever
   becomes multi-state.
2. Filter on `areasymbol LIKE 'IN%'` before the join.

**The reasoning that favoured option 2, and why it was wrong.** The argument
was that the land cover raster is clipped to Indiana and SSURGO survey areas
follow state lines, so no Indiana pixel could fall inside a neighbouring
state's polygon. That sounded right and is false.

**Decision: option 1, keep them.** Tested on Posey County, which sits on both
the Illinois and Kentucky lines — both rivers, so the hard case rather than a
convenient one — joining its land cover against every nearby polygon in
GeoPandas, a separate implementation from the Spark path:

| | |
|---|---|
| Pixels in the county's extent | 1,289,851 |
| Matched to Indiana soil | 1,287,624 |
| **Matched to out-of-state soil** | **2,227 (0.17%)** |
| **Matched to *both*** | **0** |

The last row is the decisive one. Those 2,227 pixels have no Indiana polygon
covering them at all, so filtering would not remove a redundant match — it
would convert them from matched to unmatched. Real loss, not deduplication.
They fall in Illinois and Kentucky survey areas (IL193, KY101, IL059, KY635,
IL185), which is unsurprising in hindsight: soil does not stop at a state
line even though the survey administration that maps it does.

**Consequence.** The partitioned join carries 9.5% more geometry than it
strictly needs for interior land. That is the price of not silently losing
border coverage, and it is the right way round for a project whose headline
correctness claim is a 90.21% match rate reconciled against an independent
computation. Revisit only if shuffle volume becomes the binding constraint,
and then as a stated accuracy trade rather than a free optimisation.

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
| 3a | Indiana SSURGO acquisition (§5.8) | done |
| 3b | State-scale join strategy (§5.5) | done — verified on county data |
| 3c | The Indiana run itself | **next — see §9** |
| 4 | Serving key design (§5.6) and store (§5.7) | open |
| 5 | Edge API + measured multi-region latency | open |
| 6 | Map frontend, public demo — needs §5.9 first | open |
| 7 | Scheduled weekly drought refresh (cheap, by §5.4) | stretch |

Milestone 3 is the load-bearing one: it is where the broadcast strategy of §5.3
stops working and the join has to become genuinely distributed. It also settles
the output size that §5.6 and §5.7 depend on, which is why it comes before the
serving work rather than after it.

---

## 9. Next action: run the Indiana join

Everything this needs is already on disk and committed. Nothing below requires
re-downloading anything.

### The command

```bash
.venv/bin/python -u scripts/run_join.py --aoi indiana --chunks 8 --cores 6 \
  > indiana.log 2>&1
```

- `--chunks 8` is an 8×8 = 64-block grid, putting roughly 23,000 polygons in
  each block — comfortably under the 250,000 broadcast ceiling of §5.5.
- `--cores 6` leaves 4 of 10 cores free so the machine stays usable. Drop the
  flag to use all 10 and finish faster.
- `-u` and the redirect matter: Python block-buffers to a file, so without
  `-u` the log stays empty while the job runs.

Watch it with `tail -f indiana.log`. Each block prints a line as it lands, so
progress is visible immediately and stalls are obvious.

### What to expect

Projected from the measured 93,706 records/sec at 6 cores: **20–25 minutes**
for 104,126,688 records, plus per-block overhead. This is a projection, not a
measurement — the first few block lines will give the real rate, and the run
can be killed and restarted cheaply if the rate looks wrong.

### How to know it worked

1. **`matched records` should be close to 104,126,688** and the match rate
   high. See the open question below before treating an exact 100.00% as good
   news.
2. **`precomputed rows`** is the number §5.6 and §5.7 have been waiting for —
   it determines the serving key design and the storage choice. County was
   5,349; the state figure is the one that matters.
3. **Sanity check the land cover totals** printed at the end. Corn and
   soybeans should dominate, at roughly 47% combined statewide (measured from
   the raster directly: corn 23.69%, soybeans 22.93%). A wildly different mix
   means something is wrong regardless of what the counts say.
4. `scripts/validate_join.py` validates the *county* output only. It has no
   Indiana ground truth to compare against, so it is not a check on this run.

### Two open questions this run should settle

**The 100.00% match rate.** A 50,000-point Indiana sample matched 49,838 of
49,838 — exactly 100%, where the county gets 90.21%. That is either real or a
bug, and it has not been checked.

The plausible innocent explanation: Tippecanoe's raster footprint extends
past the bounding box its soil tiles were requested for, on all four sides, so
points in that margin had no polygon available to match — an artifact of the
download extent rather than a fact about the ground. Indiana fetched all 196
tiles covering its full bounding box, leaving no such margin.

**If that explanation holds, the README is wrong.** It currently states that
the unmatched 9.8% in Tippecanoe is "open water and unsurveyed land", and
presents the agreement between two independently computed figures as
confirmation. That claim needs re-checking, because it is presented as a
correctness result and may be an artifact.

To check: take the unmatched Tippecanoe points and see whether they sit in the
margin outside the soil tile bounding box, or are scattered over water and
genuinely unsurveyed ground. The answer changes what the README should say.
