"""Download the USDA Cropland Data Layer and clip it to the area of interest.

CDL is a 30m raster where each pixel carries a crop class code. CropScape
serves it per-state; Indiana 2025 is ~144 MB. The state file is fetched once
and cached, then clipped locally to whatever AOI is configured -- cheaper and
more reproducible than re-requesting clipped extracts from the server.

This raster is where the record count comes from: Indiana is ~94,000 km2,
which at 30m is roughly 105 million pixels.
"""

import argparse
import re
import sys
import warnings
from pathlib import Path

import rasterio
import requests
from rasterio.mask import mask
from rasterio.warp import transform_bounds

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import AOIS, CDL_YEAR, DEFAULT_AOI, RAW, WGS84, resolve_aoi

SERVICE = "https://nassgeodata.gmu.edu/axis2/services/CDLService/GetCDLFile"


def _get(url: str, **kw):
    """Fetch from CropScape, tolerating their expired certificate.

    CropScape's TLS certificate expired 2026-09-10 and George Mason has not
    renewed it, so a verified request dies with CERTIFICATE_VERIFY_FAILED.
    Verified is still attempted first, so this repairs itself the moment the
    certificate is renewed -- and the fallback should be deleted once it is.

    Skipping verification is defensible *here* specifically: the payload is
    public, read-only, federal raster data carrying no credentials, and it is
    checked downstream anyway -- pixel values have to land in the CDL class
    table, and the county's crop mix is a known quantity. It would not be
    defensible on a request that authenticated or sent anything.
    """
    try:
        return requests.get(url, **kw)
    except requests.exceptions.SSLError:
        print("  !! upstream TLS certificate invalid -- retrying unverified", flush=True)
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        return requests.get(url, verify=False, **kw)


def state_raster(state_fips: str, year: int) -> Path:
    cached = RAW / f"cdl_{year}_state{state_fips}.tif"
    if cached.exists():
        print(f"already have {cached.name} ({cached.stat().st_size / 1e6:.0f} MB)")
        return cached

    print(f"asking CropScape for CDL {year}, state FIPS {state_fips} ...")
    meta = _get(SERVICE, params={"year": year, "fips": state_fips}, timeout=300)
    meta.raise_for_status()
    m = re.search(r"<returnURL>(.*?)</returnURL>", meta.text)
    if not m:
        raise SystemExit(f"unexpected CropScape response: {meta.text[:400]}")
    url = m.group(1)

    print(f"downloading {url} ...")
    with _get(url, stream=True, timeout=900) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(cached, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done / 1e6:.0f}/{total / 1e6:.0f} MB", end="", flush=True)
    print(f"\nsaved -> {cached}")
    return cached


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aoi", choices=sorted(AOIS), default=None,
                    help=f"override DEFAULT_AOI ({DEFAULT_AOI.slug}) for this run only")
    aoi = resolve_aoi(ap.parse_args().aoi)
    src_path = state_raster(aoi.state_fips, CDL_YEAR)

    out = RAW / f"cdl_{CDL_YEAR}_{aoi.slug}.tif"
    if out.exists():
        print(f"already have {out.name}, skipping clip")
        return

    with rasterio.open(src_path) as src:
        print(f"state raster: {src.width} x {src.height} px, CRS {src.crs}, dtype {src.dtypes[0]}")

        # CDL ships in Albers equal-area, not lat/lon. The AOI bbox has to be
        # projected into the raster's CRS before it can be used to clip.
        left, bottom, right, top = transform_bounds(WGS84, src.crs, *aoi.bbox)
        window = [
            {
                "type": "Polygon",
                "coordinates": [
                    [
                        (left, bottom),
                        (right, bottom),
                        (right, top),
                        (left, top),
                        (left, bottom),
                    ]
                ],
            }
        ]
        clipped, transform = mask(src, window, crop=True)
        profile = src.profile | {
            "height": clipped.shape[1],
            "width": clipped.shape[2],
            "transform": transform,
            "compress": "lzw",
        }

    with rasterio.open(out, "w", **profile) as dst:
        dst.write(clipped)

    px = clipped.shape[1] * clipped.shape[2]
    print(f"clipped to {aoi.name}: {clipped.shape[2]} x {clipped.shape[1]} px = {px:,} pixels")
    print(f"saved -> {out}  ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
