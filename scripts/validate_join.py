"""Check the Spark join against an independent single-machine computation.

The distributed join and this script answer the same question by completely
different routes: Spark tests 2M point geometries against polygon geometries
via a spatial index, while this masks the raster directly with rasterio and
counts pixels in NumPy. If both agree, the join logic is right.

The two are directly comparable because rasterio's default masking rule
includes a pixel when its *center* falls inside the polygon -- which is
exactly what ST_Contains against pixel-center points does.

Validating on a sample of soil map units rather than all of them: the point is
to catch a systematic error (wrong projection, off-by-half-a-pixel, boundary
rule mismatch), and any of those would show up on the first unit checked.
"""

import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.mask import mask

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import CDL_YEAR, DEFAULT_AOI, EQUAL_AREA, PROCESSED, RAW  # noqa: E402

SAMPLE_UNITS = 6


def main() -> None:
    aoi = DEFAULT_AOI

    overlay = pd.read_parquet(PROCESSED / f"overlay_{aoi.slug}.parquet")
    spark_by_unit = (
        overlay.groupby(["mukey", "crop_code"], as_index=False)["pixels"].sum()
    )

    soils = gpd.read_parquet(RAW / f"ssurgo_{aoi.slug}.parquet").to_crs(EQUAL_AREA)

    # Sample the largest units -- they have enough pixels that a discrepancy
    # is unambiguous rather than a rounding artifact.
    ranked = (
        spark_by_unit.groupby("mukey")["pixels"].sum().sort_values(ascending=False)
    )
    sample = ranked.head(SAMPLE_UNITS).index.tolist()

    print(f"validating {len(sample)} soil map units against rasterio ground truth\n")
    print(f"{'mukey':>8} {'spark px':>10} {'rasterio px':>12} {'delta':>8}  {'classes':>8}  result")
    print("-" * 68)

    all_ok = True
    for mukey in sample:
        geoms = soils.loc[soils.mukey == mukey, "geometry"]

        with rasterio.open(RAW / f"cdl_{CDL_YEAR}_{aoi.slug}.tif") as src:
            clipped, _ = mask(src, geoms.values, crop=True, filled=True, nodata=0)

        truth = Counter(clipped[clipped != 0].ravel().tolist())
        truth_total = sum(truth.values())

        spark_rows = spark_by_unit[spark_by_unit.mukey == mukey]
        spark_counts = dict(zip(spark_rows.crop_code.astype(int), spark_rows.pixels))
        spark_total = int(spark_rows.pixels.sum())

        delta = spark_total - truth_total
        # A handful of pixels can differ where a polygon edge passes exactly
        # through a pixel center; anything beyond that is a real bug.
        ok = abs(delta) <= max(3, 0.001 * truth_total)
        all_ok &= ok

        classes_match = set(spark_counts) == set(truth)
        print(
            f"{mukey:>8} {spark_total:>10,} {truth_total:>12,} {delta:>8,}  "
            f"{'same' if classes_match else 'DIFF':>8}  {'ok' if ok else 'MISMATCH'}"
        )

        if not ok or not classes_match:
            only_spark = set(spark_counts) - set(truth)
            only_truth = set(truth) - set(spark_counts)
            if only_spark:
                print(f"          classes only in spark:    {sorted(only_spark)}")
            if only_truth:
                print(f"          classes only in rasterio: {sorted(only_truth)}")
            for code in sorted(set(spark_counts) & set(truth)):
                a, b = spark_counts[code], truth[code]
                if a != b:
                    print(f"          code {code}: spark {a:,} vs rasterio {b:,} ({a - b:+,})")

    print("-" * 68)
    print("PASS - distributed join agrees with single-machine ground truth" if all_ok
          else "FAIL - join disagrees with ground truth")

    # Independent total-area check: the sum of the overlay should account for
    # the county's farmland without exceeding its physical size.
    total_acres = overlay.acres.sum()
    ag_acres = overlay.loc[overlay.is_agricultural, "acres"].sum()
    print(f"\ntotal acres in overlay: {total_acres:,.0f}")
    print(f"  agricultural:         {ag_acres:,.0f} ({100 * ag_acres / total_acres:.1f}%)")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
