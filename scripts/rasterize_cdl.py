"""Convert the CDL raster into columnar records for the distributed join.

Spark joins rows, not images. This step reads the GeoTIFF in blocks and emits
one row per non-empty pixel -- the pixel's center coordinate plus its land
cover code -- as a partitioned Parquet dataset.

Done as a separate step rather than inside Spark for two reasons: the raster
is a single file on one machine so there is nothing to parallelize in reading
it, and writing many Parquet parts up front gives Spark natural partitioning
to work with afterwards.

Coordinates stay in the raster's native EPSG:5070 (Albers equal-area, meters)
and are stored as int32. Pixels are 30m, so meter precision is far finer than
the data warrants, and int32 halves the size of the largest table in the
pipeline versus float64.
"""

import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from rasterio.windows import Window

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import CDL_YEAR, DEFAULT_AOI, INTERIM, RAW  # noqa: E402

# Pixels per side per output file. Sized so both AOIs land on a sensible
# partition count: a county produces a handful of files, the state ~150.
# One giant part would leave Spark nothing to parallelize across.
BLOCK = 1024

SCHEMA = pa.schema(
    [
        pa.field("x", pa.int32()),
        pa.field("y", pa.int32()),
        pa.field("crop_code", pa.uint8()),
    ]
)


def blocks(width: int, height: int, size: int):
    for row in range(0, height, size):
        for col in range(0, width, size):
            yield Window(col, row, min(size, width - col), min(size, height - row))


def main() -> None:
    aoi = DEFAULT_AOI
    src_path = RAW / f"cdl_{CDL_YEAR}_{aoi.slug}.tif"
    out_dir = INTERIM / f"cdl_points_{aoi.slug}"

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    t0 = time.time()
    written = 0
    part = 0

    with rasterio.open(src_path) as src:
        print(f"source: {src.width} x {src.height} px, CRS {src.crs}")
        transform = src.transform
        window_list = list(blocks(src.width, src.height, BLOCK))
        print(f"reading in {len(window_list)} blocks of up to {BLOCK}x{BLOCK} ...")

        for w in window_list:
            band = src.read(1, window=w)

            # Code 0 is "background" -- outside the clip footprint, not a real
            # land cover observation. Dropping it here keeps roughly a third of
            # a rectangular clip out of the join entirely.
            rows, cols = np.nonzero(band)
            if not len(rows):
                continue

            codes = band[rows, cols]

            # Pixel center, not corner: offset by half a pixel so a point
            # lands inside its own footprint rather than on the boundary
            # between four of them.
            xs, ys = rasterio.transform.xy(
                transform,
                rows + w.row_off,
                cols + w.col_off,
                offset="center",
            )

            table = pa.Table.from_arrays(
                [
                    pa.array(np.rint(xs).astype(np.int32)),
                    pa.array(np.rint(ys).astype(np.int32)),
                    pa.array(codes.astype(np.uint8)),
                ],
                schema=SCHEMA,
            )
            pq.write_table(table, out_dir / f"part-{part:05d}.parquet", compression="zstd")
            written += table.num_rows
            part += 1

    size_mb = sum(f.stat().st_size for f in out_dir.glob("*.parquet")) / 1e6
    print(f"\nwrote {written:,} pixel records across {part} parquet files")
    print(f"  -> {out_dir}  ({size_mb:.1f} MB, {time.time() - t0:.1f}s)")
    print(f"  {written / max(time.time() - t0, 1e-9):,.0f} records/sec")


if __name__ == "__main__":
    main()
