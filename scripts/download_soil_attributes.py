"""Download SSURGO map unit attributes for an AOI, keyed on mukey.

**Why this exists.** The pipeline's soil layer is `mapunitpoly` -- geometry
plus `mukey`, `musym` and `areasymbol`. Those are an identifier and a shape.
Nothing in them says what the soil *is*, so the serving tier could report how
a field divides across map units but not a single fact about the ground. This
fetches the tabular half.

**Why SDA and not another SSURGO download.** The geometry came from the WFS,
which serves spatial layers only. The tabular data lives behind Soil Data
Access, a SQL-over-REST endpoint against the same database. It is free, needs
no key, and is keyed on the same `mukey` the join already carries -- so this
is a dimension table, not a second pipeline. Nothing here touches the 455M
record join.

**The columns, and why these five.**

  muname        the human-readable name, e.g. "Iversen sandy loam, 2 to 15
                percent slopes". The one field that makes a map unit legible
                to a person rather than a 6-digit key.
  niccdcd       non-irrigated land capability class, 1-8. USDA's own judgement
                of what the ground can support without added water: 1-4 are
                cultivable, 5-8 are not. This is the interpretation layer,
                already computed by soil scientists and citable -- the
                alternative is inventing one.
  iccdcd        irrigated land capability class, same 1-8 scale. Carried
                because the AOI is California, where the great majority of
                farmed acreage is irrigated: rating Central Valley ground on
                its rainfed capability alone would understate what it
                actually supports. It is null where the ground cannot be
                irrigated at all -- which is itself an answer.
  drclassdcd    drainage class.
  aws0150wta    available water storage to 150cm, in cm. Matters next to a
                drought layer.
  slopegraddcp  representative slope gradient, percent. Also the terrain term
                in any fuel model.

**Batching.** ~21k map units is too many for one query: SDA caps result size
and the request itself is a URL-length problem long before that. They go in
chunks, and a failed chunk is retried rather than abandoned -- a partial
attribute table would silently produce fields with no capability class.

Every request carries a timeout. `download_ssurgo.py` learned that the hard
way: one dead socket with no timeout hung a 761-tile run to completion.
"""

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import AOIS, INTERIM, PROCESSED, resolve_aoi

SDA = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"

COLUMNS = [
    "mukey", "muname", "niccdcd", "iccdcd", "drclassdcd", "aws0150wta", "slopegraddcp",
]

# Chosen against the URL-length and result-size limits rather than tuned:
# 500 six-digit keys is a ~4 KB query, comfortably inside both.
BATCH = 500
TIMEOUT = 60
RETRIES = 3


def fetch(mukeys: list[str]) -> list[list[str]]:
    """One batch. Returns rows without the header SDA prepends."""
    keys = ",".join(mukeys)
    query = (
        f"SELECT {', '.join(COLUMNS)} FROM muaggatt WHERE mukey IN ({keys})"
    )
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = requests.post(
                SDA,
                json={"format": "JSON+COLUMNNAME", "query": query},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            table = r.json().get("Table")
            # SDA answers a query that matched nothing with no "Table" key at
            # all, which is not an error -- some map units genuinely have no
            # muaggatt row.
            return table[1:] if table else []
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"SDA batch failed after {RETRIES} attempts: {last}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aoi", choices=sorted(AOIS), default=None,
                    help="AOI slug; defaults to the configured default AOI.")
    args = ap.parse_args()
    aoi = resolve_aoi(args.aoi)

    overlay = PROCESSED / f"overlay_{aoi.slug}.parquet"
    if not overlay.exists():
        raise SystemExit(f"no overlay for {aoi.name} at {overlay} -- run the join first")

    # The overlay is the authority on which map units matter: it already
    # excludes the out-of-state survey areas the bounding box dragged in.
    mukeys = sorted(set(pd.read_parquet(overlay, columns=["mukey"])["mukey"].astype(str)))
    print(f"{aoi.name}: {len(mukeys):,} distinct map units")

    rows: list[list[str]] = []
    for i in range(0, len(mukeys), BATCH):
        batch = mukeys[i : i + BATCH]
        rows.extend(fetch(batch))
        done = min(i + BATCH, len(mukeys))
        print(f"  {done:>6,}/{len(mukeys):,}", end="\r", flush=True)
    print()

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["mukey"] = df["mukey"].astype(str)
    # SDA returns everything as strings, including the numerics.
    #
    # Only the capability class is genuinely integral -- it is a 1-8 ordinal.
    # Slope is a representative percentage and comes back fractional (16.5),
    # so forcing it to an int both fails loudly and would quietly lose detail
    # if it did not.
    for col in ("niccdcd", "iccdcd"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int16")
    for col in ("slopegraddcp", "aws0150wta"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop_duplicates(subset="mukey")

    out = INTERIM / f"soil_attributes_{aoi.slug}.parquet"
    df.to_parquet(out, index=False)

    missing = len(mukeys) - len(df)
    print(f"wrote {out}  ({len(df):,} rows)")
    print(f"  capability (rainfed)     : {df['niccdcd'].notna().sum():,}")
    print(f"  capability (irrigated)   : {df['iccdcd'].notna().sum():,}")
    print(f"  slope present            : {df['slopegraddcp'].notna().sum():,}")
    print(f"  water storage present    : {df['aws0150wta'].notna().sum():,}")
    if missing:
        print(f"  map units with no muaggatt row: {missing:,}")


if __name__ == "__main__":
    main()
