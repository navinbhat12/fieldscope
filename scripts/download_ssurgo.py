"""Download SSURGO soil map unit polygons for the area of interest.

Three routes into SSURGO were tested; only one is usable from a pipeline:

  - Bulk state zips are hosted behind a Box web UI. Not scriptable.
  - Soil Data Access accepts SQL, but any query asking for actual polygon
    geometry times out server-side, even over a few square miles.
  - The WFS map service returns geometry reliably. One county-sized bounding
    box came back with ~30k polygons in 21s.

So: WFS, requested in bounding-box tiles. Tiles are used rather than one giant
request because response time grows fast with area and a failure mid-download
should only cost one tile, not the whole state.

Going from a county to a state changes the shape of this job rather than just
its size: Tippecanoe is 4 tiles, Indiana is 238. At the measured ~20s a tile
that is over an hour of sequential requests against a public service, so three
things keep it tractable:

  - Tiles that miss the area's real boundary are never requested. The AOI
    bounding box is a rectangle and Indiana is not; a good part of that
    rectangle is Illinois and Ohio.
  - Tiles are fetched through a small thread pool. The pool size is bounded by
    politeness toward a government service, not by client throughput.
  - Each tile is cached to disk as it lands, so the job is resumable. A run
    that dies at tile 180 resumes at 180 instead of starting over, which is
    what makes a long download safe to interrupt.

Measure before committing to a full run:

    .venv/bin/python -u scripts/download_ssurgo.py --pilot 10
"""

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import AOIS, DEFAULT_AOI, INTERIM, RAW, WGS84, resolve_aoi

WFS = (
    "https://sdmdataaccess.sc.egov.usda.gov/Spatial/SDMWGS84Geographic.wfs"
    "?SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature"
    "&TYPENAME=mapunitpoly&SRSNAME=EPSG:4326"
)

# Roughly 0.25 degrees a side. Keeps each response in the tens-of-thousands of
# features and well inside the server's patience.
TILE_DEG = 0.25

# Concurrency against a public federal service. Four is deliberately modest:
# the goal is to stop being the slowest possible client, not to extract maximum
# throughput from someone else's infrastructure.
WORKERS = 4

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def tiles(bbox: tuple[float, float, float, float], step: float):
    minx, miny, maxx, maxy = bbox
    x = minx
    while x < maxx:
        y = miny
        while y < maxy:
            yield (x, y, min(x + step, maxx), min(y + step, maxy))
            y += step
        x += step


def aoi_footprint(aoi) -> shapely.geometry.base.BaseGeometry | None:
    """The AOI's real outline, for discarding tiles the bounding box overshoots.

    Returns None if the boundary file has not been downloaded, in which case
    every tile in the bounding box is fetched -- correct, just wasteful.
    """
    path = RAW / "counties_in.gpkg"
    if not path.exists():
        log(f"note: {path.name} missing, so no tiles can be skipped "
            f"(run download_boundaries.py first)")
        return None

    counties = gpd.read_file(path, layer="counties").to_crs(WGS84)
    if aoi.county_fips:
        counties = counties[counties.COUNTYFP == aoi.county_fips]
    return counties.geometry.union_all()


def fetch_tile(tile: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    minx, miny, maxx, maxy = tile
    url = f"{WFS}&BBOX={minx},{miny},{maxx},{maxy}"
    gdf = gpd.read_file(url)
    if not len(gdf):
        return gdf

    # This server is inconsistent about axis order, so both directions need
    # handling explicitly:
    #   - the BBOX query parameter is interpreted as lon,lat
    #   - returned geometry is lat,lon, because WFS 1.1.0 with "EPSG:4326"
    #     follows the formal EPSG axis order (latitude first) rather than the
    #     lon-first convention nearly every tool assumes
    # Left alone, the polygons land in the Indian Ocean and every downstream
    # join silently returns zero rows.
    gdf["geometry"] = shapely.transform(
        gdf.geometry.values, lambda c: c[:, ::-1], include_z=False
    )
    # Only now is the WGS84 label actually true.
    return gdf.set_crs(WGS84, allow_override=True)


def cached_tile(idx: int, pos: int, tile, cache: Path, total: int, t0: float):
    """Fetch one tile, or return the cached copy. Retries transient failures.

    `idx` is the tile's position in the whole bounding box and names its cache
    file, so the cache stays valid when a different subset is requested. `pos`
    is only how far through this run it is.
    """
    hit = cache / f"tile_{idx:04d}.parquet"
    empty = cache / f"tile_{idx:04d}.empty"

    if empty.exists():
        return None
    if hit.exists():
        log(f"  [{pos}/{total}] tile {idx}: cached")
        return gpd.read_parquet(hit)

    for attempt in range(3):
        try:
            gdf = fetch_tile(tile)
            break
        except Exception as exc:  # transient WFS failures are expected
            if attempt == 2:
                raise
            log(f"  tile {idx} attempt {attempt + 1} failed ({exc}); retrying")
            time.sleep(5 * (attempt + 1))

    if not len(gdf):
        empty.touch()
        log(f"  [{pos}/{total}] tile {idx}: empty  [{time.time() - t0:.0f}s]")
        return None

    gdf.to_parquet(hit)
    log(f"  [{pos}/{total}] tile {idx}: {len(gdf):>6} polygons  [{time.time() - t0:.0f}s]")
    return gdf


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", type=int, default=None,
                    help="fetch this many tiles, spread across the area, and report throughput")
    # The AOI is normally global config, so that switching scope is one edit
    # that moves every script together. This overrides it for this script alone,
    # which is what lets a state-scale pilot be measured without disturbing the
    # county data the rest of the pipeline is currently built against.
    ap.add_argument("--aoi", choices=sorted(AOIS), default=None,
                    help=f"override DEFAULT_AOI ({DEFAULT_AOI.slug}) for this run only")
    args = ap.parse_args()

    aoi = resolve_aoi(args.aoi)
    out = RAW / f"ssurgo_{aoi.slug}.parquet"
    if out.exists() and not args.pilot:
        print(f"already have {out.name}, skipping")
        return

    cache = INTERIM / f"ssurgo_tiles_{aoi.slug}"
    cache.mkdir(parents=True, exist_ok=True)

    all_tiles = list(tiles(aoi.bbox, TILE_DEG))
    footprint = aoi_footprint(aoi)
    if footprint is not None:
        keep = [
            (i, t) for i, t in enumerate(all_tiles, 1)
            if shapely.box(*t).intersects(footprint)
        ]
        print(f"{len(all_tiles)} tiles in bounding box, {len(keep)} intersect {aoi.name} "
              f"({len(all_tiles) - len(keep)} skipped)")
    else:
        keep = list(enumerate(all_tiles, 1))
        print(f"{len(keep)} tiles in bounding box")

    if args.pilot:
        # Spread the sample across the area rather than taking a corner: tile
        # cost tracks polygon density, which varies with land use, so a
        # contiguous block would not predict the whole run.
        stride = max(1, len(keep) // args.pilot)
        keep = keep[::stride][: args.pilot]
        print(f"PILOT: {len(keep)} tiles, spread every {stride} across the area")

    already = sum(1 for i, _ in keep
                  if (cache / f"tile_{i:04d}.parquet").exists()
                  or (cache / f"tile_{i:04d}.empty").exists())
    if already:
        print(f"{already} of {len(keep)} already cached, resuming")

    print(f"fetching with {WORKERS} workers ...")
    t0 = time.time()
    parts = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(cached_tile, i, pos, t, cache, len(keep), t0)
            for pos, (i, t) in enumerate(keep, 1)
        ]
        for f in futures:
            gdf = f.result()
            if gdf is not None and len(gdf):
                parts.append(gdf)

    elapsed = time.time() - t0
    fetched = len(keep) - already
    print(f"\n{len(keep)} tiles in {elapsed:.0f}s"
          + (f" ({fetched} fetched, {elapsed / fetched:.1f}s each)" if fetched else " (all cached)"))

    if not parts:
        raise SystemExit("no polygons returned -- nothing to write")

    soils = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=WGS84)

    # Tiles overlap at their shared edges and a polygon straddling a boundary
    # comes back in both. mupolygonkey is the stable per-polygon id.
    before = len(soils)
    soils = soils.drop_duplicates(subset="mupolygonkey").reset_index(drop=True)
    print(f"deduplicated {before} -> {len(soils)} polygons across tile seams")

    if args.pilot:
        remaining = (len(all_tiles) if footprint is None else
                     sum(1 for t in all_tiles if shapely.box(*t).intersects(footprint)))
        per_tile = elapsed / fetched if fetched else 0
        print(f"\nPILOT ESTIMATE for the full {aoi.name} run:")
        print(f"  tiles to fetch:   {remaining}")
        print(f"  measured rate:    {per_tile:.1f}s/tile at {WORKERS} workers")
        print(f"  projected time:   {remaining * per_tile / 60:.0f} min")
        print(f"  projected polys:  {len(soils) * remaining / len(keep):,.0f}")
        print("\nnot writing the AOI file -- this was a sample, not full coverage")
        return

    # Fail loudly if the geometry does not land where it was asked for. An
    # axis-order regression is otherwise invisible: the download succeeds, the
    # file looks fine, and only the join comes back empty.
    minx, miny, maxx, maxy = aoi.bbox
    gx0, gy0, gx1, gy1 = soils.total_bounds
    if not (minx - 1 <= gx0 <= maxx + 1 and miny - 1 <= gy0 <= maxy + 1):
        raise SystemExit(
            f"geometry is outside the requested area -- suspect axis order.\n"
            f"  requested bbox: {aoi.bbox}\n"
            f"  got bounds:     {(gx0, gy0, gx1, gy1)}"
        )
    print(f"bounds check ok: {tuple(round(v, 4) for v in soils.total_bounds)}")

    soils.to_parquet(out)
    print(f"saved -> {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print(f"distinct map units (mukey): {soils.mukey.nunique()}")
    print(f"survey areas: {sorted(soils.areasymbol.unique())}")


if __name__ == "__main__":
    main()
