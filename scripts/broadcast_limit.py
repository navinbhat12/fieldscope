"""Find where the broadcast join stops working, instead of assuming it does.

§5.3 of docs/DESIGN.md forces Sedona to build its spatial index over the
polygon side and broadcast it, which is what makes the county join fast. §5.5
claims that strategy cannot survive the state scale-up. This measures the
claim rather than reasoning about it.

Method: hold the point side fixed and scale only the polygon side, so the
single variable is the size of the thing being broadcast. Each trial runs in
its own subprocess with its own Spark session, because a JVM that has thrown
OutOfMemoryError cannot be trusted to run the next trial honestly -- and
because a trial that dies should cost one data point, not the sweep.

The interesting output is not "it failed" but *where* it failed and how: a
threshold and a failure mode are answerable in an interview; a binary is not.

    .venv/bin/python -u scripts/broadcast_limit.py --sweep
    .venv/bin/python -u scripts/broadcast_limit.py --polygons 300000   # one trial
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fieldscope.config import INTERIM, RAW, WGS84, resolve_aoi

EPSG_5070 = "EPSG:5070"

# Polygon counts to step through. Tippecanoe's 30,264 is the known-good floor;
# Indiana's 1,341,119 is the target. The rungs between are what turn "it
# breaks" into "it breaks somewhere between here and here".
LADDER = [30_000, 100_000, 300_000, 600_000, 1_000_000, 1_341_119]

# Points held constant across trials. Large enough that the join is real work,
# small enough that point-side cost is not what varies.
POINTS = 2_000_000


def trial(n_polygons: int, n_points: int) -> None:
    """One broadcast join at a given polygon count. Runs as a subprocess."""
    from pyspark.sql import functions as F

    from fieldscope.spark_session import build

    spark = build(app_name=f"fieldscope-bcast-{n_polygons}")
    aoi = resolve_aoi("indiana")

    # Both sides are sampled randomly, never with LIMIT. Polygons sit in tile
    # order and points in raster scan order, so LIMIT would take one corner of
    # the state and a strip of its top edge -- disjoint areas, zero matches,
    # and a timing that measures nothing but the cost of finding nothing.
    src_soils = spark.read.parquet(str(RAW / f"ssurgo_{aoi.slug}.parquet"))
    total_soils = src_soils.count()
    soils = src_soils.sample(
        withReplacement=False, fraction=min(1.0, n_polygons / total_soils), seed=42
    )
    soils.createOrReplaceTempView("s")
    polys = spark.sql(f"""
        SELECT ST_Transform(ST_GeomFromWKB(geometry), '{WGS84}', '{EPSG_5070}') AS geom, mukey
        FROM s
    """).cache()
    actual_polys = polys.count()

    src_pts = spark.read.parquet(str(INTERIM / f"cdl_points_{aoi.slug}"))
    total_pts = src_pts.count()
    pts = src_pts.sample(
        withReplacement=False, fraction=min(1.0, n_points / total_pts), seed=42
    ).selectExpr("ST_Point(CAST(x AS DOUBLE), CAST(y AS DOUBLE)) AS pt")

    t0 = time.time()
    joined = pts.join(F.broadcast(polys), F.expr("ST_Contains(geom, pt)"), "inner")
    n = joined.count()
    secs = time.time() - t0

    plan = joined._jdf.queryExecution().executedPlan().toString()
    strategy = "BroadcastIndexJoin" if "Broadcast" in plan else "OTHER"
    print(f"RESULT ok polygons={actual_polys} matched={n} secs={secs:.1f} strategy={strategy}")
    spark.stop()


def sweep() -> None:
    print(f"broadcast ladder, {POINTS:,} points held constant\n")
    print(f"{'polygons':>10}  {'outcome':<12} {'secs':>7}  detail")
    print("-" * 72)

    for n in LADDER:
        proc = subprocess.run(
            [sys.executable, "-u", __file__, "--polygons", str(n), "--points", str(POINTS)],
            capture_output=True, text=True, check=False,
        )
        line = next((x for x in proc.stdout.splitlines() if x.startswith("RESULT ok")), None)

        if line:
            f = dict(p.split("=", 1) for p in line.split()[2:])
            print(f"{n:>10,}  {'ok':<12} {float(f['secs']):>7.1f}  "
                  f"matched {int(f['matched']):,}, {f['strategy']}")
            continue

        # Failure: report the mode, not just the fact. These are the two that
        # distinguish "the broadcast itself was too big" from "the driver ran
        # out of room assembling it", which are different constraints.
        err = proc.stdout + proc.stderr
        if "OutOfMemoryError" in err:
            mode = "OOM"
        elif "Cannot broadcast" in err or "broadcast" in err.lower() and "exceed" in err.lower():
            mode = "too large"
        elif proc.returncode < 0:
            mode = f"killed (sig {-proc.returncode})"
        else:
            mode = f"exit {proc.returncode}"

        detail = next(
            (x.strip() for x in err.splitlines()
             if any(k in x for k in ("OutOfMemoryError", "Cannot broadcast", "SparkException"))),
            "",
        )
        print(f"{n:>10,}  {'FAILED':<12} {'-':>7}  {mode}: {detail[:90]}")
        print(f"\nbroadcast stops working between {LADDER[LADDER.index(n) - 1]:,} "
              f"and {n:,} polygons" if LADDER.index(n) else "\nfailed at the first rung")
        return

    print("\nbroadcast survived every rung -- §5.5's premise does not hold as stated")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true", help="step through the ladder")
    ap.add_argument("--polygons", type=int, default=None, help="run one trial at this size")
    ap.add_argument("--points", type=int, default=POINTS)
    args = ap.parse_args()

    if args.sweep or args.polygons is None:
        sweep()
    else:
        trial(args.polygons, args.points)


if __name__ == "__main__":
    main()
