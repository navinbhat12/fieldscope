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

import argparse
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq
from pyspark.sql import functions as F

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.cdl_classes import CDL_CLASSES, NON_AGRICULTURAL
from fieldscope.config import (
    AOIS,
    DEFAULT_AOI,
    EQUAL_AREA,
    INTERIM,
    PROCESSED,
    RAW,
    WGS84,
    resolve_aoi,
)
from fieldscope.spark_session import build

# Measured in scripts/broadcast_limit.py: broadcasting the polygon side works
# at 300,000 polygons and dies at 600,000 with heap exhaustion on an 8 GB
# driver. The threshold is set below the observed floor rather than between
# the two rungs, because the failure is abrupt and the cost of choosing the
# partitioned strategy unnecessarily is a slower run, not a dead one.
BROADCAST_MAX_POLYGONS = 250_000

# Settings that switch Sedona from a broadcast join to a spatially-partitioned
# one. Both sides get partitioned onto a shared KDB-tree grid and each
# partition joins locally, so nothing has to fit in the driver.
#
# KDBTREE over EQUALGRID because soil polygon density tracks land use and
# survey detail: a uniform grid would put most of the state's geometry in a
# handful of partitions and leave the rest idle, which is the whole problem
# a partitioned join is supposed to solve.
PARTITIONED_CONF = {
    "sedona.join.autoBroadcastJoinThreshold": "-1",  # never broadcast
    "sedona.join.gridtype": "kdbtree",
    "sedona.global.index": "true",
    "sedona.global.indextype": "rtree",
}

PIXEL_M2 = 30.0 * 30.0
ACRES_PER_PIXEL = PIXEL_M2 / 4046.8564224

EPSG_NUM = EQUAL_AREA.split(":")[1]
WGS84_NUM = WGS84.split(":")[1]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def stage(label: str) -> None:
    print(f"\n{'-' * 68}\n{label}\n{'-' * 68}", flush=True)


def run_chunked(spark, points_src, soils, n_chunks: int, cores: int):
    """Join block by block, so every block can use the fast broadcast path.

    Sedona's own partitioned join works and is correct -- verified against the
    broadcast result on county data -- but it was measured at roughly 900
    records/sec on Indiana, which puts the state run past thirty hours. The
    cost is in shuffling 2 GB of geometry and rebuilding indexes per partition.

    Chunking replaces that with a partition function the data already suggests.
    Each block holds few enough polygons to broadcast, so every block runs the
    ~100,000 records/sec path instead, and no geometry is shuffled at all --
    the polygon subset is small enough to send whole.

    Blocks are a regular grid rather than counties because county bounding
    boxes overlap, and a point inside two of them would be counted twice,
    silently inflating every acreage in the output. A grid partitions the
    plane exactly: each point's block is arithmetic on its coordinates, so
    double counting is impossible by construction. Polygons straddling a
    boundary go to both blocks, which is harmless -- a polygon only ever
    matches points that are themselves in that block.

    Returns (aggregated overlay rows, total matched, seconds spent joining).
    """
    bounds = points_src.agg(
        F.min("x").alias("x0"), F.max("x").alias("x1"),
        F.min("y").alias("y0"), F.max("y").alias("y1"),
    ).first()
    span_x = (bounds.x1 - bounds.x0) / n_chunks
    span_y = (bounds.y1 - bounds.y0) / n_chunks
    log(f"grid: {n_chunks}x{n_chunks} = {n_chunks ** 2} blocks, "
        f"{span_x / 1000:.0f} x {span_y / 1000:.0f} km each")

    # Polygon bounding boxes, computed once. Selecting a block's polygons is
    # then a numeric filter rather than a geometric one, which matters because
    # it happens once per block.
    boxed = soils.selectExpr(
        "geom", "mukey", "musym", "areasymbol", "drought_class",
        "ST_XMin(geom) AS bx0", "ST_XMax(geom) AS bx1",
        "ST_YMin(geom) AS by0", "ST_YMax(geom) AS by1",
    ).cache()
    boxed.count()

    # boxed carries every column the blocks need, so the original cached copy
    # is dead weight from here on. Holding both meant two copies of ~2 GB of
    # geometry pinned in the heap, which left the dense central blocks no
    # execution memory and exhausted it outright at 8 GB.
    soils.unpersist()

    t0 = time.time()
    # Blocks are rolled up in the driver as they land, rather than each block's
    # result being cached in Spark and unioned at the end. A cached DataFrame
    # per block accumulates in storage memory and steals a little more
    # execution memory on every block, so the run degrades smoothly and then
    # dies -- measured at 7s per block early and 60s for identical work by
    # block 92. The aggregates are tiny (the whole county output was 5,349
    # rows), so a plain dict holds every block at negligible cost and Spark
    # carries no growing state at all.
    totals: dict[tuple, int] = {}
    out_schema, matched, done = None, 0, 0

    for i in range(n_chunks):
        for j in range(n_chunks):
            x0 = bounds.x0 + i * span_x
            x1 = x0 + span_x if i < n_chunks - 1 else bounds.x1 + 1
            y0 = bounds.y0 + j * span_y
            y1 = y0 + span_y if j < n_chunks - 1 else bounds.y1 + 1

            blk_pts = points_src.filter(
                (F.col("x") >= x0) & (F.col("x") < x1)
                & (F.col("y") >= y0) & (F.col("y") < y1)
            )
            n_pts = blk_pts.count()
            done += 1
            if not n_pts:
                continue

            blk_soils = boxed.filter(
                (F.col("bx0") <= x1) & (F.col("bx1") >= x0)
                & (F.col("by0") <= y1) & (F.col("by1") >= y0)
            ).select("geom", "mukey", "musym", "areasymbol", "drought_class")

            pts = blk_pts.repartition(cores * 2).selectExpr(
                "ST_Point(CAST(x AS DOUBLE), CAST(y AS DOUBLE)) AS pt", "crop_code"
            )
            agg = (
                pts.join(F.broadcast(blk_soils), F.expr("ST_Contains(geom, pt)"), "inner")
                .groupBy("mukey", "musym", "areasymbol", "crop_code", "drought_class")
                .agg(F.count(F.lit(1)).alias("pixels"))
            )
            # One action per block, and nothing is retained on the Spark side.
            # The schema is captured from the first block that produces rows so
            # the final frame keeps the exact column types the join produced.
            rows = agg.collect()
            if out_schema is None and rows:
                out_schema = agg.schema

            n_hit = 0
            for r in rows:
                key = (r.mukey, r.musym, r.areasymbol, r.crop_code, r.drought_class)
                totals[key] = totals.get(key, 0) + r.pixels
                n_hit += r.pixels

            matched += n_hit
            log(f"  block {done}/{n_chunks ** 2}: {n_pts:>9,} pts -> {n_hit:>9,} matched "
                f"[{time.time() - t0:.0f}s]")

    secs = time.time() - t0
    if not totals:
        raise SystemExit("no block produced any matches")

    # The same soil-unit/crop combination appears in every block it spans, so
    # the per-block counts are summed rather than concatenated -- done above by
    # keying the dict on the group, which is the same roll-up the old union
    # performed, just carried out as each block arrives.
    rolled = spark.createDataFrame(
        [(*key, pixels) for key, pixels in totals.items()], out_schema
    )

    return rolled, matched, secs


def main() -> None:
    ap = argparse.ArgumentParser()
    # --limit N runs the whole pipeline over a sample of the land cover
    # records. Every scale-up starts here: a sample that finishes in seconds
    # proves the design before any full run is worth starting, and its
    # throughput predicts the full run's wall time.
    ap.add_argument("--limit", type=int, default=None,
                    help="run over a random sample of this many land cover records")
    ap.add_argument("--aoi", choices=sorted(AOIS), default=None,
                    help=f"override DEFAULT_AOI ({DEFAULT_AOI.slug}) for this run only")
    ap.add_argument("--strategy", choices=("auto", "broadcast", "partitioned"), default="auto",
                    help="join strategy; auto picks from the polygon count")
    # Spark takes every core by default, which pins the machine for the length
    # of the run. Capping it leaves the machine usable at the cost of wall
    # time -- worth it for a job measured in tens of minutes on a laptop that
    # is also someone's desktop.
    ap.add_argument("--cores", type=int, default=None,
                    help="limit Spark to this many cores (default: all)")
    # The 8 GB default is sized for county work. A state run caches far more
    # geometry and needs headroom, but the driver heap has to stay well under
    # physical RAM -- everything here runs in one JVM on a laptop.
    ap.add_argument("--memory", default="8g", metavar="SIZE",
                    help="driver heap, e.g. 10g (default: 8g)")
    # Chunked mode grids the area into N x N blocks and runs an independent
    # broadcast join per block. See run_chunked() for why this beats Sedona's
    # own partitioned join by roughly two orders of magnitude here.
    ap.add_argument("--chunks", type=int, default=None, metavar="N",
                    help="split the area into an N x N grid and join each block separately")
    args = ap.parse_args()

    limit = args.limit
    aoi = resolve_aoi(args.aoi)

    # The strategy has to be chosen before the session starts, because it is
    # set through Spark configuration rather than per-query. So the polygon
    # count is read from Parquet metadata first -- cheap, no session needed.
    n_polygons = pq.ParquetFile(RAW / f"ssurgo_{aoi.slug}.parquet").metadata.num_rows
    if args.strategy == "auto":
        strategy = "broadcast" if n_polygons <= BROADCAST_MAX_POLYGONS else "partitioned"
        why = f"{n_polygons:,} polygons vs a measured ceiling of {BROADCAST_MAX_POLYGONS:,}"
    else:
        strategy = args.strategy
        why = "forced with --strategy"

    spark = build(
        app_name=f"fieldscope-join-{aoi.slug}",
        local_threads=str(args.cores) if args.cores else "*",
        memory=args.memory,
        conf=PARTITIONED_CONF if strategy == "partitioned" else None,
    )
    cores = spark.sparkContext.defaultParallelism
    log(f"Spark {spark.version}, master {spark.sparkContext.master}, {cores} cores")
    log(f"AOI: {aoi.name}")
    log(f"join strategy: {strategy.upper()} ({why})")

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

    if args.chunks:
        pre_agg, n_joined, join_secs = run_chunked(spark, src, soils, args.chunks, cores)
        log(f"matched records: {n_joined:,}")
        log(f"match rate:      {100 * n_joined / n_points:.2f}% of land cover records")
        log(f"join wall time:  {join_secs:.1f}s")
        log(f"throughput:      {n_points / join_secs:,.0f} records/sec")
    else:
        pre_agg = None
        points = src.repartition(cores * 2).selectExpr(
            "ST_Point(CAST(x AS DOUBLE), CAST(y AS DOUBLE)) AS pt", "crop_code"
        )

        # Under BROADCAST, F.broadcast forces the R-tree onto the polygon side.
        # Without it Sedona indexes the points, which is both slower to build and
        # single-threaded. Under PARTITIONED there is no small side to broadcast:
        # both sides are shuffled onto a shared KDB-tree grid and each partition
        # joins locally against its own index, so the hint must be absent or
        # Spark will try to broadcast anyway and exhaust the driver.
        right = F.broadcast(soils) if strategy == "broadcast" else soils
        joined = points.join(
            right,
            F.expr("ST_Contains(geom, pt)"),
            "inner",
        ).select("crop_code", "mukey", "musym", "areasymbol", "drought_class")

        # Verify the planner actually chose what was asked for. A strategy that
        # silently falls back is the failure mode worth catching here: the job
        # still returns correct answers, just by a route that will not scale.
        plan = joined._jdf.queryExecution().executedPlan().toString()
        if "BroadcastIndexJoin" in plan:
            chosen = "BroadcastIndexJoin"
        elif "RangeJoin" in plan:
            chosen = "RangeJoin (spatially partitioned)"
        else:
            chosen = "UNKNOWN -- neither operator found in the plan"
        log(f"physical operator: {chosen}")
        if strategy == "partitioned" and "BroadcastIndexJoin" in plan:
            raise SystemExit("asked for a partitioned join and got a broadcast one -- check config")
        if strategy == "broadcast" and "SpatialIndex geom" not in plan:
            log("WARNING: R-tree is not on the polygon side, which is the slow arrangement")

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

    # Chunked mode already counted per block and summed across them, so it
    # arrives pre-grouped; the single-join path still has raw matched rows.
    counted = pre_agg if pre_agg is not None else (
        joined.groupBy("mukey", "musym", "areasymbol", "crop_code", "drought_class")
        .agg(F.count(F.lit(1)).alias("pixels"))
    )

    overlay = (
        counted
        .withColumn("acres", F.round(F.col("pixels") * F.lit(ACRES_PER_PIXEL), 3))
        .withColumn("land_cover", code_name[F.col("crop_code").cast("int")])
        .withColumn("is_agricultural", ~F.array_contains(non_ag, F.col("crop_code").cast("int")))
    ).cache()

    n_rows = overlay.count()
    # A sampled run writes somewhere else so it cannot clobber the full output.
    # validate_join.py reads the unsuffixed path and diffs it against a
    # ground truth computed over the whole raster, so a partial overlay left
    # there reports a spurious FAIL that looks like a correctness regression.
    out = PROCESSED / f"overlay_{aoi.slug}{'_sample' if limit else ''}.parquet"
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
