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
"""

import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import DEFAULT_AOI, RAW, WGS84

WFS = (
    "https://sdmdataaccess.sc.egov.usda.gov/Spatial/SDMWGS84Geographic.wfs"
    "?SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature"
    "&TYPENAME=mapunitpoly&SRSNAME=EPSG:4326"
)

# Roughly 0.25 degrees a side. Keeps each response in the tens-of-thousands of
# features and well inside the server's patience.
TILE_DEG = 0.25


def tiles(bbox: tuple[float, float, float, float], step: float):
    minx, miny, maxx, maxy = bbox
    x = minx
    while x < maxx:
        y = miny
        while y < maxy:
            yield (x, y, min(x + step, maxx), min(y + step, maxy))
            y += step
        x += step


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


def main() -> None:
    aoi = DEFAULT_AOI
    out = RAW / f"ssurgo_{aoi.slug}.parquet"
    if out.exists():
        print(f"already have {out.name}, skipping")
        return

    tile_list = list(tiles(aoi.bbox, TILE_DEG))
    print(f"fetching SSURGO for {aoi.name} in {len(tile_list)} tiles ...")

    parts, t0 = [], time.time()
    for i, tile in enumerate(tile_list, 1):
        for attempt in range(3):
            try:
                gdf = fetch_tile(tile)
                break
            except Exception as exc:  # transient WFS failures are expected
                if attempt == 2:
                    raise
                print(f"  tile {i} attempt {attempt + 1} failed ({exc}); retrying")
                time.sleep(5 * (attempt + 1))
        if len(gdf):
            parts.append(gdf)
        print(f"  tile {i}/{len(tile_list)}: {len(gdf):>6} polygons  [{time.time() - t0:.0f}s]")

    soils = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=WGS84)

    # Tiles overlap at their shared edges and a polygon straddling a boundary
    # comes back in both. mupolygonkey is the stable per-polygon id.
    before = len(soils)
    soils = soils.drop_duplicates(subset="mupolygonkey").reset_index(drop=True)
    print(f"deduplicated {before} -> {len(soils)} polygons across tile seams")

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
