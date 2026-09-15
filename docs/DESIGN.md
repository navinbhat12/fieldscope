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

**Consequence.** This also makes the weekly refresh cheap: only the
polygon→drought mapping has to be recomputed, not the full per-pixel join.
An approximation made for performance turned out to define the incremental
update path.

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
| 4 | Serving key design (§5.6) and store (§5.7) | done — decided 2026-09-14 |
| 5a | FastAPI service + Postgres/PostGIS + Redis, in Compose | **next** |
| 5b | Cache benchmark: p50/p95 cold vs warm, stated load (§5.7) | open |
| 5c | Deploy to GCP behind Cloudflare Tunnel (§5.11) | open |
| 5d | Terraform, CI — optional, not blocking (§5.11) | stretch |
| 6 | Map frontend, public demo — needs §5.9 first | open |
| 7 | Scheduled weekly drought refresh (cheap, by §5.4) | stretch |

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

### Next actions

1. **Fix the README's 9.8% claim** — see above. It is public-facing copy that
   states something the data contradicts, which is the one kind of error this
   project cannot afford.
2. **Diagnose block 92** (§5.5). Measuring polygons-per-block is cheap and
   would confirm or kill the bbox-density hypothesis; the fix, if confirmed, is
   probably to split blocks on polygon count rather than on area.
3. **§5.6 and §5.7 are now unblocked** — the row count they were waiting for is
   155,025.
