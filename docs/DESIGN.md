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
OFFLINE (batch, scheduled)              ONLINE (container)

 public datasets                         React + TypeScript + MapLibre
   CDL raster ──┐                          (Cloudflare Pages)
   SSURGO      ─┼──▶ Spark + Sedona                │ HTTPS
   USDM        ─┘      spatial join        Cloudflare (CDN, TLS, Tunnel)
                            │                       │
                            ▼                       ▼
                  overlay.parquet ──load──▶  FastAPI ──── Redis
                   155,025 rows                 │        (cache)
                                                ▼
                                       PostgreSQL + PostGIS

        GCP e2-micro · Docker Compose · Terraform · GitHub Actions
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

- ~~The serving tier needs no spatial capability at all.~~ **Revised
  2026-09-14 (§5.6).** This followed from an edge-latency target that no longer
  applies; the serving tier now uses PostGIS to resolve which precomputed rows
  a drawn field needs. The core of this decision is unaffected — the expensive
  overlay is still computed once, offline, and no request recomputes it.
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

**Consequence.** This also makes a weekly refresh cheap *in principle*: only
the polygon→drought mapping would have to be recomputed, not the full per-pixel
join. An approximation made for performance turned out to define the
incremental update path.

**In principle, because the code does not take that path — and does not need
to.** `run_join.py` attaches drought to polygons before the pixel join and then
carries `drought_class` into the final `groupBy`, so today a drought change
means re-running the whole job. Making it incremental means aggregating to
`(polygon, crop_code)` and joining drought on afterwards, turning a ~100-minute
job over 455M pixels into a seconds-long join over 484K rows.

**Descoped 2026-09-15.** Navin confirmed the demo does not need a weekly
refresh, so neither the cron job nor this reordering is being built. The
reasoning is kept because it is the correct fix if the requirement ever
returns, and because the drought layer is still a genuine part of the output —
it is simply a snapshot of one USDM week rather than a moving layer. Label it
as that week in the UI.

### 5.5 Join strategy at state scale — grid chunking

**Status:** decided, implemented, and run at state scale on 2026-09-14 (§9)

**Context.** Indiana has 1,482,366 soil polygons against 104,126,688 land
cover records, in 2.12 GB of geometry. The broadcast strategy of §5.3 cannot
survive this.

> The polygon count here previously read 1,341,119. The figure the pipeline
> actually reads from `data/raw/ssurgo_indiana.parquet` is 1,482,366, and that
> is the number every run has used. The smaller one is most likely a
> pre-§5.10 count taken before out-of-state polygons were kept, but that has
> not been confirmed. The per-block arithmetic below is recomputed at the
> larger figure, which is the conservative direction.

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
| Throughput | ~100k rec/s | ~900 rec/s | **138,559 rec/s** |

All three agree exactly, and `validate_join.py` passes against the
single-machine ground truth on the chunked output, including the known
one-pixel artifact on mukey 164315.

The chunked throughput read 93,706 rec/s until the rewrite described below
removed a per-block cache-and-count pass; 138,559 is the same measurement
(Tippecanoe, 3x3, 6 cores) taken after it, and the output is byte-identical.

**Discussion points.**

- The generic tool was the wrong tool, and only measurement showed it. Sedona's
  partitioner is general-purpose; the grid exploits something known about this
  problem — that land cover records are already uniformly distributed over a
  raster — which a general partitioner cannot assume.
- Correctness came before performance: the chunked path was verified against a
  known-good answer on county data before being pointed at the state.
- Overlapping partitions are a correctness bug, not a performance one, which is
  why counties were rejected as the chunk unit despite being the obvious choice.

**What state scale added.** The county verification above was correct and
still is, but it could not exercise the failures that actually blocked the
state run. Three defects surfaced only above roughly 250,000 polygons, and all
three were about memory rather than correctness:

1. **`--chunks` did not imply broadcast.** The strategy is chosen before the
   session exists, because Sedona reads it from Spark configuration rather
   than per query. Auto-selection saw 1,482,366 polygons, picked
   `partitioned`, and pinned `autoBroadcastJoinThreshold=-1` onto the session,
   so every per-block `F.broadcast()` was silently ignored and each block took
   the ~900 rec/s path chunking exists to avoid. The county never hit this:
   30,264 polygons is under the ceiling, so `auto` picks `broadcast` and the
   config is never set.
2. **The soil geometry was cached twice.** `run_chunked` derives `boxed` from
   `soils` and never reads `soils` again, but both stayed cached — two copies
   of ~2 GB of geometry in an 8 GB heap. The first state attempt died of heap
   exhaustion on block 37 of 64, the densest central block.
3. **Memory grew with the block count.** Each block's aggregate was cached and
   appended, then unioned only at the end, so storage memory rose on every
   block and squeezed execution memory further each time. This is the
   instructive one, because it does not look like a memory bug from the
   outside: identical 998k-point blocks ran 7s, 8s, 13s, 29s, 60s and the run
   degraded smoothly rather than failing anywhere in particular. Blocks are
   now rolled up in the driver as they land — the aggregates are tiny, 5,349
   rows for the whole county — and Spark retains nothing across blocks.

That third change was the first operation in the pipeline to need a Python
worker, every prior one being a JVM-side Sedona expression, which exposed a
fourth latent problem: `PYSPARK_PYTHON` was unset, so workers launched on the
system Python 3.9 against a 3.11 driver and Spark refuses to run across minor
versions. `spark_session.build()` now points both it and
`PYSPARK_DRIVER_PYTHON` at `sys.executable`.

**The limit of the equal-grid assumption.** Grid chunking assumes blocks are
roughly equal work, and one block of 144 violated that badly. Measured on the
completed run:

| block | wall time |
|---|---|
| 91 | 18s |
| **92** | **1,986s** |
| 93 | 58s |
| 94-100 | 2-11s each |

Block 92 took 33 minutes against 2-11s for its neighbours — roughly 400x — and
then the run recovered completely, which rules out the progressive-degradation
bugs above. It was not GC: `jstat` during the stall showed ~4% of CPU in
collection, with concurrent GC keeping up, so the JVM was doing genuine work at
a very high allocation rate. It dominated the total: 4,103s of join time, of
which one block was 1,986s. Without it the run would be ~35 minutes rather than
~69. The cause is not yet established — the likely candidate is that block's
bbox filter selecting far more polygons, or far more complex geometry, than a
typical block. A grid that equalises *points* does not equalise *polygons*, and
nothing currently measures the latter per block.

### 5.6 Serving key design — request-time spatial lookup

**Status:** decided 2026-09-14

**Context.** The overlay is keyed by
`(mukey, musym, areasymbol, crop_code, drought_class)`. That answers "what is
on this soil map unit" — but the product question is "what is under the field
I just drew," which is not the same lookup.

**Options.**

1. **Spatial query at request time** against soil polygons with a GiST index.
   Exact and straightforward, but reintroduces geometry into the request path.
2. **Grid-cell precomputation.** Bucket the output into fixed cells and store
   cell → overlay summary, so a drawn field maps to covering cells by
   arithmetic with no geometry at request time.

**Decision. Option 1.**

**Why the earlier leaning reversed.** Option 2 was preferred while the serving
tier was assumed to be an edge runtime with a sub-50ms global target, because
an edge Worker cannot hold a spatial index and a cross-region database round
trip would have dominated the budget. That constraint is gone: the serving tier
is now a container (§5.11), and the realistic audience is a small number of
people clicking a portfolio link, not a global user base. With the latency
constraint removed, option 2 is more code and a stated error budget in exchange
for an approximation of an answer PostGIS gives exactly.

**Consequences.**

- Soil polygons must live in the serving store, not just the overlay table.
  That is 1,482,366 polygons and ~2.1 GB of geometry — comfortably within
  PostGIS's normal range and within the disk budget of §5.11.
- **This revises a consequence of §5.1.** That decision concluded the serving
  tier "needs no spatial capability at all," which was true given an edge
  target and is not true now. §5.1's core claim is untouched: the expensive
  overlay is still precomputed offline, and no request recomputes it. What
  changed is only how a request finds *which* precomputed rows it needs.
- Two endpoints fall out: `GET /mapunit/{mukey}` returns one map unit's
  breakdown (7,534 of them, ~1.4 KB each), and `POST /area` takes a drawn
  GeoJSON polygon, resolves the intersecting map units spatially, and
  aggregates their overlay rows.

### 5.7 Serving store — PostgreSQL + PostGIS, with Redis in front

**Status:** decided 2026-09-14

**Context.** County output is 5,349 rows; **the Indiana output is 155,025 rows
(1.3 MB Parquet, 7,534 distinct map units)**, measured on the completed run of
§9. §5.6 adds the soil polygons themselves to the store.

**Decision.** PostgreSQL with PostGIS as the store, Redis as a read-through
cache in front of the aggregation endpoint.

**Why Postgres.** The data is relational, the queries are relational, and §5.6
needs a spatial index. Row counts are small enough that the choice is driven by
capability rather than scale.

**Why Redis is load-bearing here and would not have been before.** Under the
edge design there was nothing to cache: the store was already globally
replicated and a lookup was a single key read, so a cache in front of it would
have been decoration. §5.6 changes that. `POST /area` does real per-request
work — a spatial lookup followed by an aggregation across every map unit the
drawn polygon touches — and the query space is unbounded, because a user can
draw any polygon. That is precisely the shape a read-through cache exists for:
expensive to compute, cheap to store, repeated in practice.

**How its value must be stated.** This project will not have meaningful
traffic, so no hit-rate or latency figure from production would mean anything.
Any performance claim must be a **benchmark under stated synthetic load**,
described as such — consistent with the rule that no claim outruns a
measurement. "p50 X ms uncached, Y ms cached, at N req/s synthetic" is honest;
"serves users in Y ms" would not be.

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

### 5.9 An honest drought layer in the demo — RESOLVED

**Status:** resolved 2026-09-15 by changing the AOI, not by changing the layer.

The problem was never the drought code; it was Indiana. Measured against the
current USDM week, with the state outlines in EPSG:5070:

| Class | California | Indiana |
|---|---|---|
| D0 abnormally dry | 47.00M acres (46.4% of state) | 0.89M (3.8%) |
| D1 moderate | 17.67M acres (17.5%) | none |
| D2 severe | 1.31M acres (1.3%) | none |
| D3 / D4 | none | none |

Two-thirds of California sits in some drought class in the *current* week, at
three distinct severities. That makes the layer real without the historical
backfill option 2 below proposed, and without dating the demo to a past week —
the weekly refresh (§5.4) becomes a visibly moving layer rather than a cron job
that changes nothing. The original analysis and its options are kept below
because the reasoning still stands; it is the AOI that moved.

---

**Status (original):** open; blocks the frontend milestone

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

### 5.11 Serving stack and deployment

**Status:** decided 2026-09-14

**Context.** The batch half is done (§9). The serving half has to demonstrate
breadth — an API, a store, a frontend, a deploy — on a budget of roughly zero,
and the deployed link has to still work unattended a year from now, because its
audience is people reading a portfolio.

**Decision.**

| Layer | Choice |
|---|---|
| API | FastAPI + Pydantic + SQLAlchemy + Alembic, served by Uvicorn |
| Store | PostgreSQL + PostGIS (§5.7) |
| Cache | Redis (§5.7) |
| Frontend | React + TypeScript + MapLibre GL, on Cloudflare Pages |
| Packaging | Docker + Docker Compose |
| Host | GCP `e2-micro`, Always Free tier |
| Ingress | Cloudflare Tunnel — outbound only, no public IP on the origin |

**Not on the critical path.** Terraform and GitHub Actions are both worth
having eventually and neither is a prerequisite for anything above. Terraform
earns its keep across many resources; this is one VM, and `gcloud compute
instances create` plus `docker compose up` reaches the same place without the
detour. CI is cheap to add once there is a service to test. Add them if they
fall out naturally; do not schedule work around them.

**Why a container rather than the edge.** The edge design was chosen for global
latency. With that requirement gone (§5.6), a container is simpler, keeps the
whole system in one place, and exercises the ordinary deployment skills the
edge version skips.

**The cache benchmark is a deliverable, not a side effect.** The point of §5.7's
Redis layer is to produce a defensible number, so it is measured deliberately:
`POST /area` driven at a stated request rate over a fixed set of drawn
polygons, p50 and p95 recorded with the cache cold and warm, hit rate reported
alongside. Methodology stated with the figure, per §7. This is the one
performance claim the serving tier is expected to make, and it is honest
precisely because it is labelled a benchmark rather than production traffic.

**Corrected 2026-09-15: this deployment is not free, and cannot be.** Two
things were assumed and neither survived contact:

1. **The Always Free tier does not include an external IPv4 address.** Google's
   free-tier page lists exactly three Compute Engine items -- the `e2-micro`,
   30 GB-months of standard persistent disk, and 1 GB of North America egress.
   An address is not among them, and since Google's 2024 pricing change an
   external IPv4 in use by a running VM is billed, at roughly $3.65/month.
2. **The address cannot simply be removed.** The plan was to drop it once
   Cloudflare Tunnel was up, on the reasoning that the tunnel dials outbound so
   nothing needs an inbound address. The tunnel does dial outbound -- but a GCP
   VM with no external address has no internet **egress** either, unless a Cloud
   NAT gateway provides it, and Cloud NAT costs about $32/month. Removing the
   address to save $3.65 would either break the tunnel or cost ten times more.

So the running cost is ~$3.65/month for the address, and everything else stays
inside the free tier. It is covered by $20 of one-time Google Developer Program
credit (a Google AI Pro benefit), which is roughly five months of runway; after
that it bills to a card unless the credit is renewed or the VM is destroyed.
**Do not describe this deployment as free.** The e2-micro is free; the
deployment is not.

**Why this hosting.** GCP's Always Free tier covers one `e2-micro` with 30 GB
of disk and 1 GB of monthly egress, indefinitely — not trial credits, so it
does not consume a credit allowance that may be wanted elsewhere. Cloudflare
Tunnel exposes it without a public IP, and Pages hosts the frontend. Total
cost: zero. Durability matters more than performance here, because the failure
mode that actually hurts is a dead link months later, and expiring free tiers
are how that happens.

**Known risk.** `e2-micro` is ~1 GB of RAM for Postgres, PostGIS, Redis, and
the API together. Mitigations in order of preference: tune `shared_buffers`
down and cap Redis with `maxmemory`; `ST_Simplify` the polygons for the demo;
move to `e2-small` (~$13/month) only if the first two fail.

**Confirmed 2026-09-15: the budget is zero, so `e2-small` is off the table.**
Paying monthly for a portfolio link was rejected outright, which makes the 1 GB
ceiling a hard constraint rather than a risk to be escalated out of. The scope
moves instead: **one state, chosen for agricultural range**, rather than several.

**`ST_Simplify` is promoted from fallback to design, and it is measured.** Over
a 40,000-polygon sample of the loaded Indiana table:

| Tolerance | Geometry size | Share of raw |
|---|---|---|
| raw | 53 MB | 100% |
| 10 m | 18 MB | 34% |
| 30 m | 10 MB | **18.8%** |

At 30 m the whole soil layer drops from 2.44 GB to roughly 460 MB, which is
what makes 1 GB of RAM a workable target rather than a 2.4x overcommit.

**30 m is not a tuning knob, it is the resolution of the question.** The crop
layer is a 30 m grid, so a soil boundary resolved more finely than 30 m cannot
change any answer the API returns — the finest thing being attributed is a
single CDL pixel. Simplifying to the raster's own resolution discards
precision the output was never able to express. This costs a little area
accuracy at polygon edges and that cost should be measured against the raw
geometry before it is quoted, but it is not the "cheap, costs some precision"
compromise the line above called it.

**The answers are not simplified — only the geometry used to find them.** The
`overlay` table is 15 MB per state and holds every acre figure the API reports;
`soil_polygon` exists only to resolve a drawn polygon to a set of map units.

**That last claim is too comfortable, and the measurement says so — OPEN.**
Simplification does reach the answers, by two paths. Over the same 40,000
polygons, at a 30 m tolerance:

| | |
|---|---|
| Net area bias | **−1.07%** (simplified polygons are smaller) |
| Mean absolute error | 3.58% of total area |
| Worst single polygon | 83.8% |

The worst case is the expected one — a polygon not much wider than the
tolerance has little left to preserve — but the paths into `POST /area` matter
more than the distribution:

- `mukey_area.total_m2` is built as `SUM(ST_Area(geom))` over this very table,
  and it is the **denominator** `/area` divides by.
- The **numerator** is the intersection of the drawn polygon against the same
  geometry.

So both sides of the fraction move together and the errors partly cancel, which
is a better position than it first appears — and it argues *against* the obvious
fix. Storing an exact raw area as the denominator while intersecting simplified
geometry for the numerator would stop the cancellation and bias every fraction
low by roughly a percent. Self-consistent geometry beats a half-exact ratio.

**Measured 2026-09-15 — and the cancellation is real.** The 300 California
benchmark polygons were run through `/area` against the raw table, the table was
reloaded at a 30 m tolerance, and the same polygons re-run:

| | median | p95 | worst |
|---|---|---|---|
| Answered acres, per polygon | **-0.126%** | +1.276% | 9.583% |
| Acres per land-cover row (rows over 0.5 ac) | +0.035% | +2.525% | 33.546% |
| Coverage ratio | -0.0012 | | 0.0958 |
| Map units touched | 0 | | +/-1 |

Total across all 300: **200,469 -> 199,947 acres, -0.261%**. Four polygons of
300 gained or lost a land-cover category outright, all of them slivers.

So the geometry moves -1.07% in area but the answers move -0.261%, which is the
cancellation this section predicted, now measured rather than argued. The
worst cases are real and should not be hidden: one polygon moved 9.6%, and one
land-cover row moved 33.5%. Both are small-denominator rows where a sliver
that survived simplification in one table did not in the other.

**Decision: ship simplified.** 244 MB against 1,302 MB, for a typical answer
change of about a tenth of a percent, on a VM with 615 MB of available RAM. The
size measurement matches Indiana's prediction almost exactly: 18.7% of raw
against the 18.8% measured there.

**What may still be quoted, and what may not.** Acreage from the simplified
table is accurate to roughly a percent for a field-sized query, which is well
inside the resolution of a 30 m crop raster and fine for a demo. It is not
accurate enough to present as a survey figure, and any per-row acreage on a
sliver should be treated as indicative. `--simplify 0` still loads the geometry
as surveyed for anything that needs the exact table.

**Deliberately excluded.** Recorded because the reasons are the point:

- **Kafka** — the only recurring input is a weekly drought refresh (§5.4).
  That is a cron job. There is no event stream and no volume to justify one.
- **Kubernetes** — three containers on one host. Compose is the correct tool at
  this size; Kubernetes here would be ceremony.
- **Cassandra / ClickHouse** — 155,025 rows.
- **MySQL, Memcached** — Postgres and Redis already occupy those roles.
- **Go** — a genuine gap and a reasonable later addition as a separate focused
  service, but splitting the API across two backend languages now would buy a
  keyword rather than an improvement.

**Frontend resilience.** The frontend ships with a static snapshot of the
overlay and falls back to it when the API does not answer, so the demo link
degrades rather than breaking if the origin is ever down.

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
| 3c | The Indiana run itself | done — 2026-09-14, see §9 |
| 3d | AOI swapped to California, for range and drought | done — 2026-09-15, see §9b |
| 4 | Serving key design (§5.6) and store (§5.7) | done — decided 2026-09-14 |
| 5a | FastAPI service + PostGIS + Redis, in Compose | done — 2026-09-14, see §10 |
| 5b | Cache benchmark: p50/p95 by hit rate, stated load (§5.7) | done — 2026-09-14, see §11 |
| 5c | Deploy to GCP behind Cloudflare Tunnel (§5.11) | done — 2026-09-15, see §13 |
| 5d | Terraform, CI — optional, not blocking (§5.11) | stretch |
| 6 | Map frontend, public demo | **next** — §5.9 is closed, nothing blocks it |
| 7 | Scheduled weekly drought refresh | ~~stretch~~ descoped 2026-09-15 (§5.4) |

Milestone 3 is the load-bearing one: it is where the broadcast strategy of §5.3
stops working and the join has to become genuinely distributed. It also settles
the output size that §5.6 and §5.7 depend on, which is why it comes before the
serving work rather than after it.

---

## 9. The Indiana run

**Status:** run and validated, 2026-09-14.

### The command

```bash
.venv/bin/python -u scripts/run_join.py --aoi indiana --strategy broadcast \
  --chunks 12 --cores 6 > indiana.log 2>&1
```

`--strategy broadcast` is **required**, not optional. Without it, auto-selection
sees 1,482,366 polygons, picks `partitioned`, and pins
`autoBroadcastJoinThreshold=-1` onto the session, which silently disables the
per-block broadcast that chunking depends on — see §5.5. The earlier version of
this section omitted the flag and would have run at roughly 900 records/sec.

`--chunks 12` is a 12x12 = 144-block grid. Six blocks are empty, falling outside
the state on the bbox corners. The 8 GB driver default is correct; an attempt at
`--memory 10g` pushed a 16 GB machine into swap and made things worse.

### Measured result

| | |
|---|---|
| Land cover records | 104,126,688 |
| Soil polygons | 1,482,366 |
| Matched | 104,125,537 (99.9989%) |
| Unmatched | 1,151 |
| Output rows | 155,025 |
| Distinct map units | 7,534 |
| Output size | 1.3 MB Parquet |
| Join wall time | 4,103s |
| Total pipeline | 4,146s (69 min) |
| Throughput | 25,377 rec/s |

The throughput figure is honest but not representative: one block took 1,986s
of the 4,103s (§5.5). The other 137 non-empty blocks averaged well under 10
seconds, and the same code measures 138,559 rec/s on the county.

**Sanity check passed exactly.** Corn 23.69% and soybeans 22.93% of 23,156,980
acres, against the 23.69% / 22.93% measured independently from the raster
before the join was written. Total acreage matches Indiana's land area, and the
output has no nulls.

### The 100% match rate — resolved

The open question was whether Indiana's ~100% match rate was real or a bug,
where Tippecanoe matches only 90.21%. **It is real, and the README's
explanation of the county figure is wrong.**

The evidence is in the output itself. The README attributes the county's
unmatched 9.8% to "open water and unsurveyed land". If that were the mechanism,
Indiana could not match 99.9989%, because Indiana contains plenty of open
water — and in fact **268,118 acres of open water appear in the overlay across
5,406 rows**, meaning those pixels *did* match soil polygons. SSURGO maps water
as map units of its own; water is surveyed, not absent. So unmatched cannot
mean water.

What remains is the download-extent explanation: Tippecanoe's raster footprint
extends past the bounding box its soil tiles were requested for, so points in
that margin had no polygon available to match. Indiana fetched all 196 tiles
covering its full bounding box, leaving almost no such margin — and the 1,151
points that still miss are consistent with a thin edge effect at the state
border, not with a property of the ground.

**This makes the county's 90.21% an artifact of acquisition, not a measurement
of anything.** The README currently presents it as a correctness result, with
the agreement of two independently computed figures offered as confirmation;
both figures can agree and still describe the same artifact. The claim carries
an "under review" note and now needs rewriting.

### Follow-ups from this run

- **Diagnose block 92** (§5.5). Measuring polygons-per-block is cheap and would
  confirm or kill the bbox-density hypothesis; the fix, if confirmed, is
  probably to split blocks on polygon count rather than on area. Not blocking.
- The README's 9.8% claim was corrected on 2026-09-14 using the evidence above.

---

## 9b. The California run

**Status:** run and validated, 2026-09-15. This is the AOI the demo ships with;
§9 remains the Indiana record and Indiana's output is kept on disk.

### The command

```bash
.venv/bin/python -u scripts/run_join.py --aoi california --strategy broadcast \
  --chunks 32 --cores 6 --memory 8g
```

`--chunks 32` rather than Indiana's 12, chosen so the blocks match Indiana's in
size rather than in count: Indiana's 12x12 over a 3.35x4.02 deg box gave
23 x 39 km blocks, and 32x32 over California's 10.35x9.48 gives 22 x 38 km.
Same per-block workload, more blocks. 1024 blocks, of which roughly half fall
outside the state and cost nothing.

### Measured result

| | Indiana | California |
|---|---|---|
| Land cover records | 104,126,688 | **455,106,622** |
| Soil polygons | 1,482,366 | **484,325** |
| Matched | 104,125,537 (99.9989%) | **455,079,266 (99.9940%)** |
| Output rows | 155,025 | **311,726** |
| Distinct map units | 7,534 | **19,607** |
| Join wall time | 4,103s | **3,752.8s** |
| Total pipeline | 4,146s (69 min) | **3,871.6s (65 min)** |
| Throughput | 25,377 rec/s | **121,270 rec/s** |
| Compression | 670x | **1,460x** |

**California joined 4.4x more data in less wall time than Indiana.** The
throughput difference is mostly Indiana's block 92 (§5.5), which alone cost
1,986s of that run; but California is faster even against Indiana's
block-92-excluded rate of 49,186 rec/s, because its polygon side is 3.3x
smaller and the broadcast per block is correspondingly cheaper.

### Sanity checks against independently published figures

The Indiana run was checked against a crop mix computed from the raster before
the join was written. California admits a stronger check, because its signature
crops exist almost nowhere else and their acreage is published:

| Crop | This pipeline | Published (USDA, approx.) |
|---|---|---|
| Almonds | 1,542,209 acres | ~1.5M |
| Grapes | 914,095 acres | ~0.9M |

Neither figure was tuned. They fall out of joining a federal raster to a
federal soil survey, and land on numbers published independently of both.
Specialty crops are a sharper test than commodity totals: they occupy specific
ground, so matching their acreage means the geometry is right, not just the
arithmetic.

Two further checks pass: total area 101.21M acres against California's ~101.5M
acres of land, and agricultural land 8.93M acres (8.8%) against roughly 9.6M
acres of harvested cropland.

### The drought layer, which is why the AOI moved

| Class | Acres |
|---|---|
| D0 abnormally dry | 47.66M |
| D1 moderate | 19.31M |
| D2 severe | 1.41M |
| no drought | 32.83M |

68.4M of 101.2M acres — **67.6% of the state** — in a drought class, across
three severities. Indiana's equivalent was 3.8% in one class. §5.9 is closed by
this table.

### The slow-block skew, reproduced — and the standing hypothesis is dead

Indiana's block 92 cost 1,986s of a 4,103s run and was never explained; §5.5
guessed at bounding-box polygon density. California reproduced the shape on
completely different data, which made it diagnosable:

| | |
|---|---|
| Blocks joined | 215 at time of measurement |
| Median block | **4.0s** |
| Blocks over 30s | **3** |
| Time in those 3 | **1,458s — 61% of elapsed** |
| Slowest block | **905s** |

**Every hypothesis available from the data was ruled out by measurement.**
Block 147 took 905s; block 276 took 4s. Comparing them:

| | block 147 (905s) | block 276 (4s) |
|---|---|---|
| Points | 652,648 | 931,879 |
| Polygons intersecting | 897 | **2,079** |
| Total vertices | 176,890 | **353,448** |
| Polygons with bbox over 10% of block | 5 | 11 |
| Sum of polygon bbox area / block area | 11.4 | **18.9** |

The slow block has **fewer points, fewer polygons, fewer vertices and less
oversized-polygon coverage** than a block that ran 226x faster. Bounding-box
density is not the mechanism, and neither is raw size on any axis.

**The JVM was ruled out too**, from Spark's own metrics during the run: GC
totalled 86s of 2,647s of task time (3.3%), peak storage memory was 1.33 GB of
4.97 GB, and disk spill was zero. And it is not a warm-up effect — the first 40
blocks ran at a 2-3s median, and the stalls appeared later and in isolated
clusters with normal blocks either side.

**What is left is outside the JVM**, most likely host-level memory pressure:
the run held an 8 GB heap on a 16 GB laptop alongside Docker, and swap was
observed at 13 GB of 14.3 GB allocated with the JVM's resident size oscillating
between 3.4 and 7.1 GB. That is a hypothesis, not a finding — nothing was
sampling memory when the three stalls actually happened. A sampler was armed
afterwards (`free`/`vm_stat`/pageouts every 20s) and caught nothing, because the
run went clean from block 140 onward.

**Next step, unchanged in spirit but now much narrower:** run the join again
with the memory sampler armed from the start and with Docker stopped, and see
whether the stalls survive the removal of host memory pressure. That is a
cheaper and more decisive experiment than anything geometric.

### Other follow-ups

- Indiana's `overlay_indiana.parquet` is untouched, so both states can be
  served once the loader appends instead of truncating.

---

## 10. The serving tier — built

**Status: steps 1-5 are done and committed (2026-09-14); step 6, the deploy, is
not.** This section was written as a build plan for a session with no prior
context, and is kept as the record of what was built and in what order. Where
the plan met something it did not know, the amendments are recorded at the end
of this section rather than by editing the steps. §11 has the benchmark.

**What runs today:**

```bash
docker compose up -d
docker compose run --rm api python scripts/load_serving.py   # ~2.3 min
```

That gives `GET /mapunit/{mukey}`, `POST /area`, `POST /ping` (a benchmark
control) and `GET /health`, over 155,025 overlay rows, 1,482,366 soil polygons
and 10,013 per-map-unit area totals. The load is idempotent: re-running
reproduces those counts exactly.

Everything below is milestone 5, and the decisions it implements are §5.6 (key
design), §5.7 (store) and §5.11 (stack).

**Do not re-run the Spark join.** `data/processed/overlay_indiana.parquet`
already exists — 155,025 rows, 1.3 MB — and is the input to step 2.

### Step 1 — Compose skeleton

Create `docker-compose.yml` at the repo root with three services:

- `db` — `postgis/postgis:16-3.4`, volume-backed, `shared_buffers` tuned low
  (the deploy target is a 1 GB VM, §5.11)
- `cache` — `redis:7-alpine`, `--maxmemory 128mb --maxmemory-policy allkeys-lru`
- `api` — built from `serving/Dockerfile`, depends on both

Keep the batch pipeline's `pyproject.toml` untouched; the service gets its own
dependency set under `serving/`.

### Step 2 — Loader

`scripts/load_serving.py`, idempotent and re-runnable:

1. `data/processed/overlay_indiana.parquet` → table `overlay`
   (`mukey, musym, areasymbol, crop_code, land_cover, is_agricultural,
   drought_class, pixels, acres`), indexed on `mukey`.
2. `data/raw/ssurgo_indiana.parquet` → table `soil_polygon`
   (`mukey`, `geom` in EPSG:5070), with a **GiST index on `geom`**. This is
   1,482,366 rows and ~2.1 GB; expect it to be the slow part.

Schema managed by Alembic so the deploy is reproducible.

### Step 3 — API

FastAPI under `serving/`, two endpoints from §5.6:

- `GET /mapunit/{mukey}` — one map unit's land cover breakdown. 7,534 of these,
  ~1.4 KB each, median 21 rows.
- `POST /area` — takes a GeoJSON polygon, transforms it to EPSG:5070 (§5.2),
  finds intersecting map units via the GiST index, aggregates their `overlay`
  rows into a single breakdown weighted by intersected area.

`POST /area` is the expensive one and the reason the cache exists.

### Step 4 — Cache

Redis read-through on `POST /area` only. Key = a hash of the **normalised**
polygon — round coordinates to a fixed precision before hashing, or trivially
different drawings of the same field will miss. No TTL needed: the underlying
data changes only when the batch pipeline reruns, so invalidate by flushing on
load rather than by expiry.

### Step 5 — Benchmark (milestone 5b)

The deliverable, per §5.7. Drive `POST /area` over a fixed set of drawn
polygons at a stated request rate; record p50 and p95 with the cache cold and
warm, and the hit rate. **State the methodology with the figure and label it a
benchmark under synthetic load** — this project has no real traffic, and a
latency number presented as production behaviour would be false (§7).

### Step 6 — Deploy (milestone 5c)

`gcloud compute instances create` for a GCP `e2-micro` on the Always Free tier,
`docker compose up`, then `cloudflared` for ingress so the origin needs no
public IP. Terraform and CI are explicitly **not** prerequisites (§5.11).

### Not yet, and why

The React frontend comes after §5.9, which is still open: Indiana has
essentially no current drought, so the layer is truthful and empty and the demo
looks broken. That decision shapes what the frontend renders, so it should be
settled before the frontend is built — but it blocks none of steps 1-6.

### Amendments made during the build — 2026-09-14

Recorded here rather than by silently editing the steps above, because in each
case the plan met something the plan did not know.

**The soil Parquet is EPSG:4326, not EPSG:5070.** Step 2 above asserts 5070.
It is wrong: `scripts/download_ssurgo.py` requests 4326 from the WFS and stores
it that way, and `scripts/run_join.py:331` transforms to 5070 at join time
rather than on disk. The loader therefore reprojects on load and the
`soil_polygon` column is `geometry(MultiPolygon, 5070)`. This matters for more
than tidiness: acres computed in 4326 are meaningless, and storing 5070 is what
makes serving acres agree with batch acres by construction rather than by
coincidence.

**A third table, `mukey_area`.** `POST /area` weights each map unit's
breakdown by the fraction of that unit the drawn polygon covers. The
denominator is the unit's total area across every polygon of it in the state,
and a map unit is not one shape but many scattered ones. Computing that per
request means scanning all of a unit's geometry purely to divide by it, which
is the exact shape of work the batch/serving split exists to move offline. It
is 7,534 rows, built once at load.

**Alembic dropped for now.** §5.11 lists it. There are three tables, one writer,
and no data that cannot be regenerated from Parquet; the recovery for any
schema change is drop-and-reload, which is precisely what migrations exist to
avoid needing — so here they would be ceremony. The schema is versioned as
plain DDL in `serving/schema.sql` and `serving/indexes.sql`, which keeps the
deploy reproducible. **Revisit when a loaded VM exists** and reloading costs an
hour of someone's evening rather than three minutes of a laptop's.

**The PostGIS image is `imresamu/postgis`, not `postgis/postgis`.** Every tag of
the official image is amd64-only. On an Apple Silicon development machine that
means the database runs under QEMU emulation, which is ruinous for the one step
that dominates the load. `imresamu/postgis:16-3.4` is the multi-arch build from
the maintainer of the official docker-postgis images, same PostgreSQL 16 and
PostGIS 3.4, and it runs natively both here and on the amd64 deploy target — so
this is one image across both, not a local-only substitution. Rosetta was
considered and is unnecessary: nothing in the stack is amd64-only any more.

**`POST /area` returns an estimate, and says so in its own response.** The batch
join collapsed pixel locations into per-map-unit totals (§5.1), so knowing a
field covers 30% of a map unit, the API can only report 30% of that unit's land
cover. That is exact only if land cover is distributed uniformly within the
unit, which it is not. Recovering the true answer would mean putting the raster
back in the request path — the entire thing this design exists to avoid. The
limitation ships in the `method` field of every response rather than living
only in this document, because a caller should not have to read a design doc to
learn that a number is approximate.

**A size cap on the drawn polygon.** `MAX_QUERY_ACRES`, default 100,000 —
roughly 156 square miles, against about 80 acres for a large Indiana field. It
is checked by a pre-flight query that reprojects and measures the polygon
without touching a table, so an oversized request is rejected before the
expensive spatial work rather than after it.

**§5.9 is worse than "sparse".** `drought_class` across all 155,025 rows takes
exactly two values: `-1` and `0`. There is no drought anywhere in the Indiana
output — the layer is not thin, it is empty. Whatever §5.9 decides has no
useful data to render in this AOI.

### Planned: more than one state — requested 2026-09-14

Indiana alone cannot demonstrate the product. It is uniformly humid corn-and-
soy on deep glacial soils with **no drought at all** (§5.9), so two of the three
datasets show their full range and the third shows nothing. Adding a small
number of contrasting states is therefore a functional requirement, not
coverage for its own sake.

**Not before the Indiana vertical slice is serving.** This is recorded so the
schema and the API stay state-agnostic while they are being written — which
they now are: nothing in `serving/` hardcodes Indiana, and both loader inputs
are selected by `--aoi`.

**What each candidate would exercise**, and why the set should stay small:

| State | Adds |
|---|---|
| Kansas or Nebraska | Recurrent drought — makes the USDM layer non-empty. Irrigated circles against dryland wheat. |
| California (Central Valley) | Specialty crops (almonds, vines), heavy irrigation, and the country's most severe drought record. |
| Arizona or New Mexico | Rangeland and desert — mostly non-agricultural land, which tests that `is_agricultural` means something. |
| Mississippi or Arkansas | Delta soils, rice and cotton — a cropping system unlike the Midwest's. |

**The binding constraint is storage, and it is already measurable.** Indiana's
soil geometry alone is ~2.3 GB loaded, against 30 GB of Always Free disk
(§5.11). Three or four states is plausible; the whole country is not, on this
hosting. Two levers exist if it gets tight: `ST_Simplify` on the served
geometry (cheap, costs some area precision), or storing soil geometry only for
the states actually demoable. Decide with measured table sizes, not estimates.

**Superseded 2026-09-15 — one state, swapped rather than added.** The budget for
hosting is zero, which fixes the host at an `e2-micro` and its 1 GB of RAM
(§5.11). Rather than accept a degraded demo across several states, the AOI
becomes a *single* state chosen for agricultural range, and Indiana is replaced
rather than joined.

**The estimate above was wrong, and measurement is why we know.** This section
asserted that "California's SSURGO is substantially larger than Indiana's."
`download_ssurgo.py --aoi california --pilot 10` says otherwise:

| | Indiana | California |
|---|---|---|
| Soil polygons | 1,482,366 | 442,369 (projected from 10 tiles) |
| Tiles intersecting the state | 238 | 761 |
| Measured fetch rate | ~20s/tile | 1.0s/tile at 4 workers |
| Projected download | ~1 hr | 13 min |

California covers 4.5x Indiana's land area with **3.3x fewer** soil polygons,
because SSURGO's detail follows survey intensity rather than area: Indiana is
uniformly row-cropped land mapped at fine grain, while California's deserts,
rangeland and mountains are mapped as very large units. Bounding-box area was
the wrong proxy and predicted the answer backwards.

The consequence is that the expensive side of a California run is the raster,
not the vector: ~470M CDL pixels against Indiana's 104M, joined to a
*smaller* polygon set.

**A drought state changes the refresh story too.** §5.4's weekly USDM refresh is
currently a cron job that changes nothing, because Indiana is never in drought.
Against Kansas it becomes a visible, moving layer — which is a considerably
better demonstration of why the pipeline reruns at all.

## 11. The serving benchmark

Milestone 5b. **Everything here is synthetic load against a local Docker
Compose stack.** Fieldscope has no users, so no figure below describes
production behaviour, and none is presented as though it does (§7).

### Method

- **A fixed, committed polygon set** — `scripts/bench_polygons.json`, 300
  field-sized squares centred on randomly sampled Indiana map units that have
  overlay coverage, in five size classes. The cost of `POST /area` scales with
  how many map units a polygon touches, so one polygon size would measure one
  shape of query rather than the endpoint. The set is committed so runs are
  comparable across machines and dates, and it is larger than a run's request
  count so every request in a cold run is a genuine first touch.
- **Open-loop load.** Requests are issued on a fixed schedule at a target rate
  rather than one after another. Closed-loop sending lets a slow server slow
  the offered load, which hides precisely the queueing the rate exists to
  expose. Achieved rate is reported next to the target; all runs held 50.0/s.
- **Five runs per phase, the first discarded**, then the median of the per-run
  medians with the full observed range. A single sample is not a claim.
- **A no-op control.** `POST /ping` takes the identical request body and
  returns immediately, measuring everything `/area` pays that is not the query:
  HTTP, ASGI, Pydantic validation, JSON serialisation, and Docker's port
  forwarding. Without it, any statement about what caching saves is unfounded,
  because the difference could be dominated by transport no cache can remove.

### Three checks that the numbers are real

Each of these could have invalidated the result, and each was run rather than
argued:

1. **Is the load generator the bottleneck?** It sustained 200 req/s at p50
   3.07 ms with no errors. At 50 req/s it is nowhere near its limit, so the
   figures describe the server.
2. **Is the tail just connection-pool queueing?** Quadrupling the pool
   (`DB_POOL_SIZE` 5 → 20) moved p95 from 63.90 ms to 63.61 ms — inside the
   run-to-run range. The tail is genuine PostGIS work, not contention.
3. **Is the harness internally consistent?** A cold request does identical
   database work to an uncached one plus a cheap Redis write, so the two must
   coincide. They do: p50 8.52 vs 7.80 ms, p95 63.53 vs 63.90 ms.

### What it measures, and what it does not

**The floor dominates the median.** The control costs 5.03 ms at p50, against
7.80 ms uncached and 6.14 ms warm. Subtract it and the spatial query is ~2.8 ms
while a cache hit is ~1.1 ms — the cache saves 1.66 ms at the median, which is
close to nothing. This is the opposite of the story a serving tier is usually
sold with, and it is the measured one.

**A caveat that cuts against the flattering reading.** `/ping` returns a tiny
body while `/area` returns up to ~20 breakdown entries, so the floor excludes
response serialisation and is a *lower bound* on overhead. The true non-query
cost is higher, which makes "caching barely helps the median" stronger.

**The percentage depends on which framing is chosen, so both are stated.**
Raw p95 falls 63.90 → 9.20 ms, an 85.6% reduction. With the floor subtracted
that is 56.35 → 1.65 ms, 97% of the server's tail work removed. The first is
what a caller experiences; the second is what the cache actually did. Quoting
only the second would be flattery.

**And both of those describe a 100% hit rate, which is not a result.** The
warm phase hits on every request; no real workload does. Because p95 *is* the
percentile where misses live, the honest measurement is a mixed one, so the
benchmark drives a stated hit rate:

| Phase | Hit rate | p50 | p95 | p99 |
|---|---|---|---|---|
| Baseline (control) | — | 5.03 ms | 7.55 ms | 9.1 ms |
| Uncached | — | 7.80 ms | 63.90 ms | 220.5 ms |
| Cold | 0% | 8.52 ms | 63.53 ms | 223.0 ms |
| Mixed | 50% | 7.30 ms | 23.98 ms | not reported |
| Mixed | 90% | 7.49 ms | 12.81 ms | not reported |
| Warm | 100% | 6.14 ms | 9.20 ms | 9.8 ms |

p95 improves smoothly with hit rate — 63.9 → 24.0 → 12.8 → 9.2 ms — so **the
figure worth quoting is the 50% one: a 62% lower p95** at a hit rate a real
workload might actually reach. The median hardly moves at any hit rate, because
the median was never the problem.

**Why the mixed rows report no p99.** Warm and cold polygons are split by index,
so at a 90% hit rate only 30 distinct polygons are ever missed and whether a
pathologically slow one falls in that subset is luck. p95 is stable across runs
(12.44–16.69 ms); p99 would be an artifact of the split. A stratified or random
split would fix this and has not been done.

**The distribution is heavy-tailed, which is the real finding.** Uncached p99 is
220 ms and the slowest single request observed was 616 ms, against a 7.8 ms
median — nearly two orders of magnitude. A small number of polygons touch enough
map units to cost vastly more than a typical one. **Which polygons, and why, is
not measured.** That is the obvious next investigation, and it rhymes with the
unexplained block 92 of the Indiana join (§5.5): in both cases a small subset of
the workload dominates the total, and in neither case does anything yet count
what makes those cases different.


## 13. The deploy

**Status: live, 2026-09-15.** Milestone 5c.

### What runs

| | |
|---|---|
| Host | GCP `e2-micro`, `us-west1-b`, project `fieldscope-demo` |
| Disk | 30 GB `pd-standard` (**not** the billable `pd-balanced` default) |
| Memory | 969 MB total; 685 MB used with the stack up, 284 MB available |
| Database | 295 MB — 311,726 overlay rows, 484,325 polygons, 20,811 map units |
| Services | `db`, `cache`, `api` via Compose, all `restart: unless-stopped` |
| Ingress | `cloudflared` quick tunnel, systemd unit `fieldscope-tunnel` |
| Cost | ~$3.65/month for the external IPv4 (§5.11) |

```bash
gcloud compute ssh fieldscope --zone=us-west1-b --project=fieldscope-demo
cd ~/fieldscope
sudo docker compose -f docker-compose.yml -f docker-compose.vm.yml ps
sudo systemctl status fieldscope-tunnel
sudo grep -ohE 'https://[a-z0-9-]+\.trycloudflare\.com' /var/log/cloudflared.log | head -1
```

### How it was built, and why that way

**The database was shipped as a `pg_dump -Fc -Z9`, not re-loaded.** 166 MB over
the wire, restored in **37 seconds** including every index. Running
`load_serving.py` on the VM instead would have meant reprojecting 484,325
polygons on a shared vCPU — the local run takes 1.4 min on an M2 Pro, and the
GiST build is the slowest part of it. Shipping the finished artefact is both
faster and repeatable.

**The simplified table is what made it fit.** 244 MB against 1,302 MB raw
(§5.11). With 284 MB of memory free once the stack is up, the raw table would
have thrashed a `pd-standard` disk on every uncached query. This is the
deployment constraint the simplification work was for, and it was measured, not
assumed.

**`docker-compose.vm.yml` binds every service to loopback.** Nothing is
published to the VM's public address; `cloudflared` runs on the host and dials
out. Each `ports`/`volumes` key carries `!override`, which is load-bearing —
Compose merges sequences by appending, so without it the base file's
`0.0.0.0` bindings survive alongside the loopback ones.

### The URL is temporary, and that is a known gap

The tunnel is a **quick tunnel**, so the hostname is random and changes on every
restart of the service. Cloudflare says plainly it is not for production. It
proves the deployment works end to end and it costs nothing.

A stable hostname needs a domain in a Cloudflare account, which is the one
remaining paid dependency (~$10/year) and was declined for now. Converting is a
`cloudflared tunnel create` plus a DNS record and a config file; nothing else in
this section changes.

### Not done here

**The benchmark has not been re-run on this hardware.** §11's figures are from
an M2 Pro with 6 cores. This is a shared vCPU with 284 MB free, and the numbers
will be worse. They must not be quoted next to the deployed link until measured
on it — `scripts/benchmark_serving.py` with
`scripts/bench_polygons_california.json` is the command, and the polygon set is
now AOI-correct so the measurement will mean something.

---

## 12. Next steps

Rewritten 2026-09-15 at end of session. Milestone numbers refer to §8.

**Where the project actually is.** Both halves are built, measured and
deployed. The batch pipeline runs at state scale against California
(§9b), the serving tier answers over the public internet (§13), and the
three open design questions that were blocking — the drought layer (§5.9),
the simplification cost (§5.11), and the AOI choice — are all closed with
measurements. **The only thing missing from the product is the frontend.**

**1. The frontend (milestone 6). This is the whole remaining product.**
React + TypeScript + MapLibre GL on Cloudflare Pages. Nothing blocks it:
§5.9 is closed, the API is live, and Pages is free and needs no card.

  - **It has to be sleek, modern and clean, and designed for geo/map data
    specifically.** This is Navin's explicit requirement and the surface
    anyone evaluating the project actually sees. A competent, responsive map
    is worth more here than another backend feature.
  - Draw a field boundary, `POST /area`, render the crop / soil / drought
    breakdown. A Central Valley field returns ~67 land-cover categories, so
    the breakdown needs real information design, not a table dump — that is
    a presentation problem, and it is the interesting part.
  - Ships with a static snapshot of the overlay so the demo degrades rather
    than breaking if the origin is down (§5.11).
  - Label the drought layer with its USDM week: it is a snapshot, not a live
    feed (§5.4, descoped).
  - The API base URL must be an environment variable — the quick-tunnel
    hostname changes on every tunnel restart (§13).

**2. Re-run the benchmark — and understand that three variables moved, not
one.** §11's chart measures Indiana, on raw geometry, on an M2 Pro. The
deployed system is California, on simplified geometry, on a shared vCPU.
Nothing about it transfers, and no latency figure may appear beside the
deployed link until this runs. Now meaningful, because the polygon set follows
the AOI (`scripts/make_bench_polygons.py`).

| | benchmarked (§11) | deployed (§13) |
|---|---|---|
| Hardware | M2 Pro, 6 cores, 17 GB | shared vCPU, 969 MB, ~284 MB free |
| Storage | NVMe | `pd-standard` |
| AOI | Indiana — 155,025 rows, 1.48M polygons | California — 311,726 rows, 484K polygons |
| Geometry | raw, 2.44 GB | simplified, 244 MB |

**A prediction, recorded before measuring so it can be wrong.** `e2-micro` is
burstable with a 0.25 vCPU baseline and `pd-standard` is slow, so the *uncached*
path should get substantially worse. A cache hit is a Redis lookup regardless of
CPU, so the *cached* path should barely move. If both hold, the cache's value
looks **better** on the VM than on the laptop — the 62% tail reduction should
grow, not shrink. The simplified table cuts the other way (10x less data to
scan), so the uncached regression may be smaller than the hardware alone
suggests.

**Measure server-side and client-side separately.** Running the harness on the
VM against `127.0.0.1:8000` isolates the service. Running it from a laptop
against the tunnel measures what a visitor experiences, Cloudflare round trip
included. Both are worth having; they are not the same number and must not be
labelled as though they were.

**Do not merge the new numbers into §11's chart.** Indiana-on-laptop and
California-on-VM differ in three variables at once, so shared axes would imply a
comparison neither supports — the exact failure mode §7 exists to prevent. If a
comparison is wanted, run the 2x2 and label every series with its full
configuration:

| | laptop | VM |
|---|---|---|
| California, simplified | isolates the hardware change | the deployed figure |
| Indiana, raw | §11 as it stands | not worth running |

Two extra runs separate "what the hardware cost" from "what the data changed",
which is a better story than either number alone.

**3. A stable hostname.** The quick tunnel's URL changes on restart, which is
fine for a proof and wrong for a resume link. Needs a domain on Cloudflare
(~$10/year), which was declined; revisit when the frontend is worth linking to.

**4. Add Indiana back alongside California.** Its overlay is on disk and
already computed, so the expensive half is free. Indiana's dense fine-grained
soil under uniform corn/soy against California's coarse soil under 67 crops
makes a stronger claim than either alone.

**Disk is not the constraint; RAM is.** Measured and projected:

| | simplified |
|---|---|
| California geometry | 244 MB (measured) |
| Indiana geometry | ~459 MB (2.44 GB at the measured 18.8%) |
| Both overlays + `mukey_area` | ~47 MB |
| **Total** | **~750 MB** |

Against 27 GB of free disk that is nothing, and against ~284 MB of available
RAM it is nearly 3x over. The hot path stays fine — `overlay` and `mukey_area`
together are 47 MB and will stay resident, so cached answers are unaffected. It
is the **cold** path that pays: a GiST lookup plus intersection against geometry
that no longer fits in page cache, on slow `pd-standard`. Measure the uncached
p95 before and after adding the second state rather than assuming it is
tolerable; if it is not, load Indiana at a coarser tolerance, since it is the
secondary state.

Three changes needed:
- `load_serving.py` `TRUNCATE`s all three tables, so it replaces rather than
  appends. It needs a per-AOI load that leaves other states alone.
- The API must scope by state. `areasymbol` already carries it — note the
  California download legitimately contains AZ, NV and OR survey areas from the
  bounding box (§5.10), so the scoping key is the AOI the row was loaded under,
  not the survey-area prefix.
- **The frontend has a real design problem here**, not just a toggle: two
  regions two thousand miles apart with nothing in between. A state selector, a
  zoomed-out US view with two lit regions, or separate entry points are all
  plausible; this is a presentation decision and belongs with the frontend work.

**5. Explain the cache result.** Deferred deliberately until the slice was up;
it sharpens a number that is already defensible rather than unblocking
anything. The three questions and the cheap first step are unchanged: why the
median is already fast, what makes p95 slow, and how a warm cache removes it.

**6. The slow-block skew (§5.5).** Reproduced on California and materially
narrowed — see that section for what was ruled out. Still open.

**Explicitly not next.** Terraform and CI (§5.11) remain optional. Alembic
stays unnecessary. The weekly drought refresh is descoped (§5.4). More states
beyond a second are out of scope.
