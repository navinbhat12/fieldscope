"""Download county boundaries from the Census cartographic boundary files.

These aren't one of the three analysis layers — they just define the area of
interest precisely (a real county outline rather than a hand-typed bounding
box) and give the demo something to snap field queries to.
"""

import io
import sys
import zipfile

import geopandas as gpd
import requests

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import RAW, WGS84, resolve_aoi

CB_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"


def outline_path(aoi) -> "Path":
    """Where this AOI's county outline lives.

    Named by state FIPS so a second state does not overwrite the first. The
    original Indiana file predates this convention and is still honoured.
    """
    legacy = RAW / "counties_in.gpkg"
    if aoi.state_fips == "18" and legacy.exists():
        return legacy
    return RAW / f"counties_{aoi.state_fips}.gpkg"


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aoi", default=None)
    aoi = resolve_aoi(ap.parse_args().aoi)
    out = outline_path(aoi)
    if out.exists():
        print(f"already have {out.name}, skipping")
        return

    # The Census archive covers every state, so a previous run's extraction
    # serves any new AOI without touching the network again.
    extracted = sorted((RAW / "_cb_counties").glob("*.shp")) if (RAW / "_cb_counties").exists() else []
    if extracted:
        print(f"reusing already-extracted {extracted[0].name}")
        shp_path = extracted[0]
    else:
        print(f"downloading county boundaries from Census ({CB_URL.rsplit('/', 1)[-1]}) ...")
        resp = requests.get(CB_URL, timeout=300)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            shp = next(n for n in zf.namelist() if n.endswith(".shp"))
            zf.extractall(RAW / "_cb_counties")
        shp_path = RAW / "_cb_counties" / shp

    counties = gpd.read_file(shp_path).to_crs(WGS84)
    state = counties[counties.STATEFP == aoi.state_fips]
    state.to_file(out, layer="counties", driver="GPKG")

    print(f"saved {len(state)} {aoi.name} counties -> {out.name}")

    # A whole-state AOI has no county to single out; report the state outline
    # instead. Reaching for a county here is what broke the first non-county run.
    target = state[state.COUNTYFP == aoi.county_fips] if aoi.county_fips else state
    if aoi.county_fips:
        print(f"AOI county: {target.NAME.iloc[0]} ({len(target)} feature)")
    print(f"  actual bounds: {tuple(round(v, 4) for v in target.total_bounds)}")
    print(f"  declared bbox: {aoi.bbox}")


if __name__ == "__main__":
    main()
