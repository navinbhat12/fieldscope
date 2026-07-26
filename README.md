# Fieldscope

A distributed data pipeline that joins tens of millions of geographic records
across several large public datasets, precomputes the result, and serves it
from a globally cached API.

Given any field boundary, the API returns everything known about the ground
underneath it — soil composition, crop cover, and current drought conditions —
as a single low-latency lookup, rather than as an expensive geometric
computation performed per request.

> **Status: in development.** The batch pipeline is being built now. Numbers
> below marked _(measured)_ come from actual runs; everything else is not yet
> built and is described as a target. No performance claim appears here until
> it has been benchmarked.

---

## The problem

The three source datasets describe the same land but disagree about how to
divide it up. Soil survey data is irregular polygons averaging a few acres.
Crop cover is a uniform 30-meter grid. Drought severity is a handful of
country-sized shapes. Answering "what is under this field" means reconciling
all three geometrically.

Doing that per request is far too slow to serve interactively. So the work is
split: an expensive offline join runs on Spark and collapses the answer into a
compact lookup table, and the online API only ever does key lookups against
that precomputed result.

This batch/serving split is the core design decision — the heavy computation
and the low-latency serving path have completely different scaling
characteristics and are deliberately decoupled.

## Architecture

```
OFFLINE (batch, scheduled)              ONLINE (always on, edge)
┌────────────────────────────┐          ┌────────────────────────────┐
│ Public datasets            │          │ Cloudflare Workers API     │
│  · crop cover (raster)     │          │  · key lookups only        │
│  · soil survey (vector)    │   load   │  · KV cache for hot keys   │
│  · drought severity        │  ─────▶  │                            │
│         │                  │          │         │                  │
│         ▼                  │          │         ▼                  │
│ Apache Spark + Sedona      │          │ React map frontend         │
│  distributed join over     │          │  draw a field, see the     │
│  ~10^8 records             │          │  overlay                   │
│         │                  │          └────────────────────────────┘
│         ▼                  │
│ Compact lookup table       │
│  (GeoParquet → PostGIS/D1) │
└────────────────────────────┘
```

## Scale

Scope is Indiana. The pipeline is developed against a single county for fast
iteration, then run unchanged across the state.

| | Tippecanoe County _(measured)_ | Indiana (target) |
|---|---|---|
| Crop-cover records | 2,080,830 | ~105,000,000 |
| Soil polygons | 30,264 | ~800,000 |
| Distinct soil map units | 429 | — |
| Raw input size | ~40 MB | ~1–2 GB |

## Data sources

All public, federal, and actively maintained. No synthetic data anywhere in
the pipeline.

| Dataset | Source | What it provides |
|---|---|---|
| Cropland Data Layer | USDA NASS | 30m crop classification, annual |
| SSURGO | USDA NRCS | Field-verified soil survey polygons |
| Drought Monitor | NDMC / NOAA / USDA | Weekly drought severity, D0–D4 |
| County boundaries | US Census | Area-of-interest definition |

Each is fetched by a script in `scripts/`, so the full input set is
reproducible from a clean checkout. Access quirks that cost real debugging
time are documented in comments at the top of each script.

## Repository layout

```
scripts/     one script per dataset, plus the exploration report
src/         pipeline package; config.py defines the areas of interest
data/        gitignored — everything here is re-downloadable
notebooks/   exploration
docs/        design notes
```

## Running it

Requires Java 17 and Python 3.11. Both are pinned deliberately: Spark 3.5 and
Sedona 1.7 are not tested against newer runtimes and fail in unhelpful ways.

```bash
uv sync

.venv/bin/python scripts/download_boundaries.py
.venv/bin/python scripts/download_cdl.py
.venv/bin/python scripts/download_ssurgo.py
.venv/bin/python scripts/download_drought.py

.venv/bin/python scripts/explore.py     # sanity report over the downloaded data
```

To switch from county to statewide, change `DEFAULT_AOI` in
`src/fieldscope/config.py`. Nothing else changes.

## Progress

- [x] Data acquisition — all four sources scripted and verified
- [x] Exploration and cross-layer validation
- [ ] Spark + Sedona distributed join
- [ ] Serving store
- [ ] Edge API and latency benchmarking
- [ ] Map frontend
- [ ] Scheduled weekly refresh of the drought layer
