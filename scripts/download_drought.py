"""Download the current US Drought Monitor weekly snapshot.

USDM publishes every Thursday, national coverage, D0-D4 severity polygons.
The file is tiny (~2 MB) and the whole country fits comfortably, so this one
is never clipped at download time.

Two URL details, both found by getting 404s:
  - the path requires a trailing `_M` on the date stamp
  - the stamp is the *Tuesday* the map is valid for, not the Thursday it is
    published. Asking for Thursday dates 404s on every week.
"""

import io
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import RAW, WGS84

BASE = "https://droughtmonitor.unl.edu/data/shapefiles_m/USDM_{stamp}_M.zip"


def recent_valid_dates(n: int = 4):
    """USDM maps are stamped with the Tuesday they are valid for.

    That map is published the following Thursday, so the most recent Tuesday
    may not exist yet. Walk backwards until one resolves.
    """
    # Local date rather than UTC is deliberate: the worst a timezone boundary can
    # do is shift the first candidate by a week, and the caller walks backwards
    # through candidates until one resolves, so it self-corrects.
    today = date.today()  # noqa: DTZ011
    tuesday = today - timedelta(days=(today.weekday() - 1) % 7)
    return [tuesday - timedelta(weeks=i) for i in range(n)]


def main() -> None:
    for d in recent_valid_dates():
        stamp = d.strftime("%Y%m%d")
        url = BASE.format(stamp=stamp)
        print(f"trying USDM release {d.isoformat()} ...")
        resp = requests.get(url, timeout=180)
        if resp.status_code != 200:
            print(f"  {resp.status_code}, trying previous week")
            continue

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            shp = next(n for n in zf.namelist() if n.endswith(".shp"))
            zf.extractall(RAW / "_usdm")

        drought = gpd.read_file(RAW / "_usdm" / shp).to_crs(WGS84)
        out = RAW / "drought_current.parquet"
        drought.to_parquet(out)

        print(f"saved {len(drought)} drought polygons (valid {stamp}) -> {out}")
        print(f"columns: {list(drought.columns)}")
        print(f"severity classes present: {sorted(drought.DM.unique())}")
        return

    raise SystemExit("no USDM release found in the last 4 weeks")


if __name__ == "__main__":
    main()
