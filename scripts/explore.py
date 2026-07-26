"""Phase 1 sanity report: do these three layers actually describe the same place?

Prints CRS, record counts, schemas, and spatial overlap for every layer, plus
the CDL class breakdown. The point is to catch misalignment now -- before any
Spark code exists -- rather than debugging an empty join later.
"""

import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import CDL_YEAR, DEFAULT_AOI, EQUAL_AREA, RAW  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> None:
    aoi = DEFAULT_AOI

    counties = gpd.read_file(RAW / "counties_in.gpkg", layer="counties")
    county = counties[counties.COUNTYFP == aoi.county_fips]
    county_geom = county.geometry.iloc[0]

    rule(f"AOI: {aoi.name} County, Indiana")
    area_km2 = county.to_crs(EQUAL_AREA).area.iloc[0] / 1e6
    print(f"area: {area_km2:,.0f} km2")
    print(f"bounds: {tuple(round(v, 4) for v in county.total_bounds)}")

    # ---------------------------------------------------------------- CDL
    rule("Layer 1 - CDL (crop land cover, raster)")
    with rasterio.open(RAW / f"cdl_{CDL_YEAR}_{aoi.slug}.tif") as src:
        print(f"CRS:        {src.crs}  <- NOT lat/lon")
        print(f"size:       {src.width} x {src.height} = {src.width * src.height:,} pixels")
        print(f"resolution: {abs(src.transform.a):.0f} m per pixel")
        print(f"dtype:      {src.dtypes[0]}  (pixel value = crop class code)")
        band = src.read(1)
        cdl_bounds_ll = gpd.GeoSeries.from_wkt(
            [
                f"POLYGON(({src.bounds.left} {src.bounds.bottom},"
                f"{src.bounds.right} {src.bounds.bottom},"
                f"{src.bounds.right} {src.bounds.top},"
                f"{src.bounds.left} {src.bounds.top},"
                f"{src.bounds.left} {src.bounds.bottom}))"
            ],
            crs=src.crs,
        ).to_crs("EPSG:4326")

    counts = Counter(band.ravel().tolist())
    nonzero = {k: v for k, v in counts.items() if k != 0}
    total = sum(nonzero.values())
    print(f"\nnon-empty pixels: {total:,}")
    print("top 12 classes by pixel count:")
    for code, n in sorted(nonzero.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  code {code:>3}: {n:>9,}  ({100 * n / total:5.1f}%)")
    print(f"distinct classes present: {len(nonzero)}")

    # ------------------------------------------------------------- SSURGO
    rule("Layer 2 - SSURGO (soil map units, vector)")
    soils = gpd.read_parquet(RAW / f"ssurgo_{aoi.slug}.parquet")
    print(f"CRS:      {soils.crs}")
    print(f"polygons: {len(soils):,}")
    print(f"distinct map units (mukey): {soils.mukey.nunique():,}")
    print(f"columns:  {[c for c in soils.columns if c != 'geometry']}")
    print(f"geometry types: {dict(Counter(soils.geom_type))}")
    acres = soils.muareaacres.astype(float)
    print(f"polygon size (acres): min {acres.min():.2f}  median {acres.median():.2f}  max {acres.max():,.0f}")

    in_county = soils[soils.intersects(county_geom)]
    print(f"\npolygons intersecting {aoi.name} County: {len(in_county):,}")

    # ------------------------------------------------------------ drought
    rule("Layer 3 - US Drought Monitor (severity, vector, national)")
    drought = gpd.read_parquet(RAW / "drought_current.parquet")
    print(f"CRS:      {drought.crs}")
    print(f"polygons: {len(drought)} (one multipolygon per severity class, national)")
    print(f"severity classes (DM): {sorted(drought.DM.tolist())}  [0=D0 abnormally dry .. 4=D4 exceptional]")

    hit = drought[drought.intersects(county_geom)]
    if len(hit):
        print(f"\nclasses overlapping {aoi.name} County: {sorted(hit.DM.tolist())}")
    else:
        print(f"\n*** NO drought polygons overlap {aoi.name} County right now ***")
        state_geom = counties.union_all()
        state_hit = drought[drought.intersects(state_geom)]
        print(f"    classes overlapping Indiana statewide: {sorted(state_hit.DM.tolist()) or 'none'}")

    # ---------------------------------------------------------- alignment
    rule("Cross-layer spatial alignment")
    print(f"CDL extent (reprojected to lat/lon): {tuple(round(v, 4) for v in cdl_bounds_ll.total_bounds)}")
    print(f"SSURGO extent:                       {tuple(round(v, 4) for v in soils.total_bounds)}")
    print(f"County extent:                       {tuple(round(v, 4) for v in county.total_bounds)}")
    overlap = cdl_bounds_ll.intersects(soils.union_all()).iloc[0]
    print(f"\nCDL footprint intersects SSURGO footprint: {overlap}")
    print(f"Distinct CRS across layers: CDL={5070}, others=4326 -> reprojection required before join")


if __name__ == "__main__":
    main()
