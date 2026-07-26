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

from fieldscope.config import DEFAULT_AOI, RAW, WGS84  # noqa: E402

CB_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"


def main() -> None:
    aoi = DEFAULT_AOI
    out = RAW / "counties_in.gpkg"
    if out.exists():
        print(f"already have {out.name}, skipping")
        return

    print(f"downloading county boundaries from Census ({CB_URL.rsplit('/', 1)[-1]}) ...")
    resp = requests.get(CB_URL, timeout=300)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        shp = next(n for n in zf.namelist() if n.endswith(".shp"))
        zf.extractall(RAW / "_cb_counties")

    counties = gpd.read_file(RAW / "_cb_counties" / shp).to_crs(WGS84)
    state = counties[counties.STATEFP == aoi.state_fips]
    state.to_file(out, layer="counties", driver="GPKG")

    target = state[state.COUNTYFP == aoi.county_fips]
    print(f"saved {len(state)} Indiana counties -> {out}")
    print(f"AOI county: {target.NAME.iloc[0]} ({len(target)} feature)")
    print(f"  actual bounds: {tuple(round(v, 4) for v in target.total_bounds)}")


if __name__ == "__main__":
    main()
