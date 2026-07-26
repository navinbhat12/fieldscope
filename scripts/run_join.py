"""The distributed join: land cover records against soil and drought polygons.

This is the expensive computation the whole architecture exists to move
offline. Every land cover pixel is matched to the soil map unit it falls
inside and the drought severity covering it, then collapsed into a compact
table the serving layer can answer from with lookups instead of geometry.

Shape of the work:

    land cover records   (2.1M county / ~105M state)   point
              x
    soil polygons        (30k county / ~800k state)    polygon   -> contains
              x
    drought polygons     (5, national)                 polygon   -> contains

A naive comparison would be ~10^11 point/polygon tests at county scale. The
job avoids that with three decisions, each of which was worth an order of
magnitude and none of which Spark makes correctly on its own:

1. INDEX THE SMALL SIDE. Left to itself, Sedona built its R-tree over the
   2.1M points and broadcast that -- a single-threaded index build over the
   largest table in the job. Broadcasting the polygons instead builds the tree
   over 30k geometries and streams points through it in parallel.

2. PARTITION THE BIG SIDE. The land cover records arrive as a handful of
   Parquet files, and Spark runs one task per file, so only that many cores
   ever engage regardless of how many the machine has.

3. KEEP THE COARSE LAYER OUT OF THE PER-POINT PATH. Drought is 5 rows, but
   they are national multipolygons -- 1,332 parts and 178k vertices. Testing
   every point against them dominated everything else. Drought resolution is
   enormous compared to a 3-acre soil polygon, so severity is attached to
   soil polygons instead and inherited by the points inside them.

Everything runs in EPSG:5070 (Albers equal-area, meters): the largest table is
already native to it, and area is expressible in real units, which degrees
cannot do.
"""

import sys
import time
from pathlib import Path

from pyspark.sql import functions as F

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.cdl_classes import CDL_CLASSES, NON_AGRICULTURAL  # noqa: E402
from fieldscope.config import DEFAULT_AOI, EQUAL_AREA, INTERIM, PROCESSED, RAW, WGS84  # noqa: E402
from fieldscope.spark_session import build  # noqa: E402

PIXEL_M2 = 30.0 * 30.0
ACRES_PER_PIXEL = PIXEL_M2 / 4046.8564224

EPSG_NUM = EQUAL_AREA.split(":")[1]
WGS84_NUM = WGS84.split(":")[1]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def stage(label: str) -> None:
    print(f"\n{'-' * 68}\n{label}\n{'-' * 68}", flush=True)


def main() -> None:
    # --limit N runs the whole pipeline over a sample of the land cover
    # records. Every scale-up starts here: a sample that finishes in seconds
    # proves the design before any full run is worth starting, and its
    # throughput predicts the full run's wall time.
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    aoi = DEFAULT_AOI
    spark = build(app_name=f"fieldscope-join-{aoi.slug}")
    cores = spark.sparkContext.defaultParallelism
    log(f"Spark {spark.version}, master {spark.sparkContext.master}, {cores} cores")

    t_start = time.time()

    # ------------------------------------------------------------- inputs
    stage("Loading inputs")

    raw_points = spark.read.parquet(str(INTERIM / f"cdl_points_{aoi.slug}"))

    # Sampling is random, not a head(). The records are written in raster scan
    # order, so LIMIT returns a thin horizontal strip of the top edge -- which
    # both understates the match rate and makes throughput meaningless, since
    # spatially clustered points only ever probe a few branches of the R-tree.
    sample_fraction = None
    if limit:
        total = raw_points.count()
        sample_fraction = min(1.0, limit / total)
        raw_points = raw_points.sample(withReplacement=False, fraction=sample_fraction, seed=42)
        log(f"SAMPLE MODE: random {100 * sample_fraction:.2f}% of {total:,} records")

    n_points = raw_points.count()
    log(f"land cover records: {n_points:,} in {raw_points.rdd.getNumPartitions()} partitions")

    raw_soils = spark.read.parquet(str(RAW / f"ssurgo_{aoi.slug}.parquet"))
    raw_soils.createOrReplaceTempView("raw_soils")
    log(f"soil polygons:      {raw_soils.count():,}")

    raw_drought = spark.read.parquet(str(RAW / "drought_current.parquet"))
    raw_drought.createOrReplaceTempView("raw_drought")
    log(f"drought polygons:   {raw_drought.count():,}")

    # --------------------------------------------------- drought -> soils
    stage("Attaching drought severity to soil polygons")

    minx, miny, maxx, maxy = aoi.bbox
    aoi_wkt = (
        f"POLYGON(({minx} {miny},{maxx} {miny},{maxx} {maxy},{minx} {maxy},{minx} {miny}))"
    )

    # Clip the national drought layer to the area of interest before it touches
    # anything else. Outside the AOI its geometry is pure cost.
    spark.sql(f"""
        SELECT
            ST_Transform(
                ST_Intersection(ST_GeomFromWKB(geometry), ST_GeomFromText('{aoi_wkt}')),
                'EPSG:{WGS84_NUM}', 'EPSG:{EPSG_NUM}'
            ) AS geom,
            CAST(DM AS INT) AS drought_class
        FROM raw_drought
        WHERE ST_Intersects(ST_GeomFromWKB(geometry), ST_GeomFromText('{aoi_wkt}'))
    """).createOrReplaceTempView("drought_aoi")

    n_drought = spark.table("drought_aoi").count()
    log(f"drought polygons overlapping AOI: {n_drought}")

    spark.sql(f"""
        SELECT
            ST_Transform(ST_GeomFromWKB(geometry), 'EPSG:{WGS84_NUM}', 'EPSG:{EPSG_NUM}') AS geom,
            mukey, musym, areasymbol
        FROM raw_soils
    """).createOrReplaceTempView("soils_proj")

    if n_drought:
        # Worst severity touching the polygon. A soil polygon straddling a
        # drought boundary reports the more severe of the two, which is the
        # conservative answer for a risk overlay.
        soils = spark.sql("""
            SELECT s.geom, s.mukey, s.musym, s.areasymbol,
                   COALESCE(MAX(d.drought_class), -1) AS drought_class
            FROM soils_proj s
            LEFT JOIN drought_aoi d ON ST_Intersects(d.geom, s.geom)
            GROUP BY s.geom, s.mukey, s.musym, s.areasymbol
        """)
    else:
        log("no drought in AOI -- skipping the join entirely")
        soils = spark.sql("SELECT *, -1 AS drought_class FROM soils_proj")

    soils = soils.cache()
    log(f"soil polygons carrying drought: {soils.count():,}")

    # --------------------------------------------------------- the join
    stage("Distributed spatial join")

    # One task per input file leaves most cores idle. Spread the big side
    # across ~2 partitions per core so every core has work and stragglers
    # have somewhere to even out.
    src = spark.read.parquet(str(INTERIM / f"cdl_points_{aoi.slug}"))
    if sample_fraction:
        src = src.sample(withReplacement=False, fraction=sample_fraction, seed=42)
    points = src.repartition(cores * 2).selectExpr(
        "ST_Point(CAST(x AS DOUBLE), CAST(y AS DOUBLE)) AS pt", "crop_code"
    )

    # F.broadcast forces the R-tree onto the polygon side. Without it Sedona
    # indexes the points, which is both slower to build and single-threaded.
    joined = points.join(
        F.broadcast(soils),
        F.expr("ST_Contains(geom, pt)"),
        "inner",
    ).select("crop_code", "mukey", "musym", "areasymbol", "drought_class")

    plan = joined._jdf.queryExecution().executedPlan().toString()
    indexed_side = "polygons" if "SpatialIndex geom" in plan else "points (SLOW)"
    log(f"R-tree built over: {indexed_side}")

    t_join = time.time()
    joined = joined.cache()
    n_joined = joined.count()
    join_secs = time.time() - t_join

    log(f"matched records: {n_joined:,}")
    log(f"match rate:      {100 * n_joined / n_points:.2f}% of land cover records")
    log(f"join wall time:  {join_secs:.1f}s")
    log(f"throughput:      {n_points / join_secs:,.0f} records/sec")

    # ---------------------------------------------------- precomputed table
    stage("Aggregating to the serving table")

    code_name = F.create_map([F.lit(x) for kv in CDL_CLASSES.items() for x in kv])
    non_ag = F.array([F.lit(c) for c in sorted(NON_AGRICULTURAL)])

    overlay = (
        joined.groupBy("mukey", "musym", "areasymbol", "crop_code", "drought_class")
        .agg(F.count(F.lit(1)).alias("pixels"))
        .withColumn("acres", F.round(F.col("pixels") * F.lit(ACRES_PER_PIXEL), 3))
        .withColumn("land_cover", code_name[F.col("crop_code").cast("int")])
        .withColumn("is_agricultural", ~F.array_contains(non_ag, F.col("crop_code").cast("int")))
    ).cache()

    n_rows = overlay.count()
    out = PROCESSED / f"overlay_{aoi.slug}.parquet"
    overlay.coalesce(1).write.mode("overwrite").parquet(str(out))

    log(f"precomputed rows: {n_rows:,}")
    log(f"compression:      {n_points:,} inputs -> {n_rows:,} rows ({n_points / n_rows:,.0f}x)")
    log(f"written -> {out}")

    stage("Largest overlaps (soil map unit x land cover)")
    overlay.orderBy(F.desc("acres")).select(
        "mukey", "musym", "land_cover", "is_agricultural", "drought_class", "pixels", "acres"
    ).show(15, truncate=False)

    stage("Land cover totals across the AOI")
    (
        overlay.groupBy("land_cover", "is_agricultural")
        .agg(F.round(F.sum("acres"), 0).alias("acres"))
        .orderBy(F.desc("acres"))
        .show(12, truncate=False)
    )

    log(f"total pipeline wall time: {time.time() - t_start:.1f}s")
    spark.stop()


if __name__ == "__main__":
    main()
