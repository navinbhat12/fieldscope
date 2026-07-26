# Progress & Handoff

**Last updated:** 2026-07-26
**Phases complete:** 1 (data acquisition), 2 (distributed join)
**Next phase:** 3 — serving store, then 4 — edge API
**Decision on record:** ship an end-to-end demo on county data first; the
Indiana scale-up comes after. See "Decisions already made" below — do not
re-litigate these without reading the reasoning.

This file exists so a session starting cold can resume without re-deriving
anything. Read it alongside `PROJECT_BRIEF.md` (local only, gitignored — it is
Navin's private planning doc and must never be committed).

---

## 1. Current state in one paragraph

Four public datasets download reproducibly via scripts. A Spark + Sedona job
joins 2,079,440 land cover records against 30,264 soil polygons and the
national drought layer for Tippecanoe County, Indiana, in 56.6 seconds, and
its output has been validated against an independent single-machine
computation. Nothing is deployed or served yet. No latency number exists, and
none should be claimed until measured.

## 2. Measured results (real numbers — safe to cite)

| Metric | Value |
|---|---|
| Land cover records | 2,079,440 |
| Soil polygons | 30,264 (429 distinct map units) |
| Records matched to a soil unit | 1,875,956 (90.21%) |
| Join wall time | 56.6s |
| Throughput | 36,717 records/sec |
| Precomputed output rows | 5,349 (389:1 compression) |
| Raw input on disk | ~40 MB |
| Hardware | 10-core local Spark session (`local[*]`) |

The 90.21% match rate is **not** data loss. Soil polygons cover 90.3% of the
raster footprint; the rest is open water and unsurveyed land. Those two
figures were computed by different methods and agree to within 0.1%.

Land cover mix for the county comes out 63% corn and soybeans, which is
correct for Tippecanoe and is a useful smell test after any pipeline change.

## 3. Environment

Pinned deliberately. Spark 3.5 and Sedona 1.7 are not tested against newer
runtimes and fail in unhelpful ways.

- **Java 17** at `/opt/homebrew/opt/openjdk@17` (installed via brew alongside
  the system Java 20, which is *not* compatible)
- **Python 3.11** in `.venv`, managed by `uv` (system Python is 3.13, also not
  compatible)
- Sedona JARs resolve from Maven on first run and cache in `~/.ivy2` (~121 MB,
  one-time). First Spark start after a clean checkout is slow because of this.

```bash
uv sync                      # rebuild the venv
.venv/bin/python -u <script> # ALWAYS use -u, see section 7
```

## 4. Pipeline — how to run it end to end

```bash
# Phase 1 — acquisition (~2 min total, mostly the 144 MB CDL download)
.venv/bin/python scripts/download_boundaries.py
.venv/bin/python scripts/download_cdl.py
.venv/bin/python scripts/download_ssurgo.py
.venv/bin/python scripts/download_drought.py
.venv/bin/python scripts/explore.py        # cross-layer sanity report

# Phase 2 — join
.venv/bin/python scripts/rasterize_cdl.py  # raster -> 2.08M Parquet rows
.venv/bin/python -u scripts/run_join.py --limit 100000   # ALWAYS sample first
.venv/bin/python -u scripts/run_join.py    # full run, ~65s
.venv/bin/python -u scripts/validate_join.py
```

`data/` is gitignored; everything in it rebuilds from the scripts above.

Switching scope from county to state is a one-line change: `DEFAULT_AOI` in
`src/fieldscope/config.py`. Read section 6 first — the join will not survive
that change as currently written.

## 5. Decisions already made (with reasoning — don't silently reverse these)

**Scope is Indiana, never CONUS or global.** Navin was explicit. The "10M+
records" claim in the brief does not require national coverage: the record
count comes from CDL raster pixels, not soil polygons, and Indiana alone is
~105M pixels (94,000 km² at 30m). Tippecanoe is ~2.1M, which is under the
claim — hence the state run still matters, but only the state run.

**Join runs in EPSG:5070 (Albers equal-area, meters), not lat/lon.** The
largest table is already native to 5070 so it needs no reprojection, and area
in degrees is meaningless — "acres of corn on this soil type" needs real
units.

**Drought severity is attached to soil polygons, not to individual pixels.**
Drought regions are enormous compared to a 3-acre soil polygon, so this is a
negligible approximation and it keeps a 178k-vertex national layer out of the
per-pixel path. A soil polygon straddling a drought boundary takes the more
severe class, which is the conservative answer for a risk overlay.

**Build/validate on Tippecanoe, scale to Indiana once.** Fast iteration, and
results are hand-checkable against a single-machine computation.

**`PROJECT_BRIEF.md` and `CLAUDE.md` are gitignored.** The brief is career
strategy — it names the roles Navin is targeting and explains that this
project replaces a resume entry. It must not end up in a repo a recruiter
might read. `CLAUDE.md` only contains the import of it.

**Repo is private.** `github.com/navinbhat12/fieldscope`. Flip when ready:
`gh repo edit navinbhat12/fieldscope --visibility public --accept-visibility-change-consequences`

## 6. Blockers and known problems

### 6a. The join will not scale to Indiana as written — must fix before Phase 6

The county run is fast *because* the 30k soil polygons broadcast cheaply, with
the R-tree built over the small side. Indiana has ~800k soil polygons,
extrapolating to ~65M vertices and close to a gigabyte of geometry. That
cannot be broadcast to every executor — it will blow the broadcast threshold
or OOM the driver.

The state run needs Sedona's **spatially-partitioned join** instead: both
sides partitioned onto a shared grid (KDB-tree or quadtree), each partition
joining locally, no broadcast. This is real work, not a config flag.

Do not trust the naive extrapolation (105M ÷ 36,717/sec ≈ 48 min) — it assumes
a join strategy that will not run at that size.

### 6b. Indiana currently has essentially no drought

Tippecanoe has none; statewide it is only D0 ("abnormally dry"). The pipeline
handles this correctly and truthfully returns no drought, but **that layer
will render empty in the demo**. Fix when building the frontend: also load a
historical week from the USDM archive (2012 was a severe drought year in
Indiana) so the UI has something to show. The archive goes back to 1999 and
the URL pattern is the same.

### 6c. Serving-store decision is still open

PostGIS vs. Cloudflare D1, deliberately deferred until output size was known.
It now is: **5,349 rows for a county**. Extrapolating to Indiana gives
low-hundreds-of-thousands of rows — comfortably inside D1. But see section 8;
the serving key design has to be settled first, because it changes the row
count.

## 7. Process lessons — please actually follow these

These cost ~25 minutes of wasted time in the last session.

- **Always sample before a full run.** `run_join.py --limit 100000` finishes in
  ~14s. A design that is wrong at 100k is wrong at 2M; find out in seconds.
- **Always `python -u`, and never pipe through `grep`/`head` while waiting.**
  Python block-buffers stdout to a file, and those tools buffer again. Both
  together mean a running job produces a completely empty log and you are
  debugging blind.
- **Always put a watchdog on a long job** so it cannot run indefinitely:
  ```bash
  ( sleep 480 && pkill -f 'fieldscope-join' ) & WD=$!
  .venv/bin/python -u scripts/run_join.py > run.log 2>&1
  kill $WD 2>/dev/null
  ```
- **macOS has no `timeout`** — use the pattern above.
- **Sample randomly, never with `LIMIT`.** Records are written in raster scan
  order, so `LIMIT` returns a thin strip of the top edge. That understated the
  match rate as 31% and made throughput numbers fiction.
- **Navin wants running commentary on what is being run and why.** Do not
  execute long silent sequences of exploratory commands. Say what is being
  checked before checking it, and flag explicitly when something is unplanned
  exploration versus a step in the agreed plan.

## 8. Next steps — Phase 3, in order

**The first real decision is the serving key**, because it determines the
shape of everything downstream. Currently the overlay is keyed by
`(mukey, musym, areasymbol, crop_code, drought_class)`. That answers "what is
on this soil unit" but not directly "what is under the field I just drew,"
which is the actual product question. Two options:

- **Spatial query at request time** against soil polygons in PostGIS with a
  GiST index. Straightforward, but every request does geometry, and hitting
  Postgres from a Worker makes the sub-50ms claim harder.
- **Grid-cell precomputation** — bucket the join output into fixed cells,
  store cell → overlay summary in D1 or KV, and have the API do pure key
  lookups. A drawn field maps to covering cells with arithmetic, no geometry
  at request time. Much better fit for the edge story and for the brief's
  "under 50ms worldwide" target.

Recommend the grid-cell approach. Note the wrinkle: cells must be computable
in a Worker from lon/lat, so either grid in lat/lon directly, or accept doing
an Albers transform in JS.

Then, in order:

1. Settle the serving key; add a grid-cell aggregation to `run_join.py`.
2. Load the precomputed table into the chosen store (D1 likely — see 6c).
3. Build the Cloudflare Workers API. Benchmark real latency from multiple
   regions. **The resume claim needs a measured number, not an estimate.**
4. Frontend on Cloudflare Pages — map, draw or select a field, show the
   overlay. Load a historical drought week so that layer is not empty (6b).
5. Only then: fix the join strategy (6a) and do the Indiana scale-up to get
   the headline record count.
6. Stretch: weekly drought refresh via Cloudflare Cron Trigger.

## 9. Things deliberately NOT claimed yet

Nothing public asserts a latency figure, and the README says so explicitly.
The repo description says "100M+ records," which is the Indiana projection,
not a measurement — it is the only forward-looking claim anywhere public. The
brief requires measured numbers in the resume bullet, so benchmark before
writing anything.
