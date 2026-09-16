"""Load the precomputed overlay and its soil geometry into PostGIS.

Runs inside the api container, which is the one environment holding both a
Postgres driver and a Parquet reader:

    docker compose run --rm api python scripts/load_serving.py

Idempotent: every table is truncated and rebuilt, so re-running converges on
the same state rather than doubling rows. The soil load is the expensive part
-- 1,482,366 polygons and ~2 GB of WKB, reprojected from EPSG:4326 to the
equal-area EPSG:5070 the batch join used (docs/DESIGN.md §5.2) -- so it is
streamed in chunks through an unlogged staging table rather than materialised
in memory, and it can be skipped with --only overlay while iterating on the
API.

Indexes are built last, on purpose: maintaining a GiST index across 1.48M
inserts costs far more than building it once over a finished table.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg
import pyarrow.dataset as ds

# Layout inside the container: /app/data and /app/scripts are bind mounts, and
# the DDL is baked into the image next to the app.
APP = Path(__file__).resolve().parents[1]
DATA = APP / "data"
SCHEMA_SQL = APP / "schema.sql"
INDEXES_SQL = APP / "indexes.sql"

OVERLAY_COLUMNS = [
    "mukey", "musym", "areasymbol", "crop_code",
    "drought_class", "pixels", "acres", "land_cover", "is_agricultural",
]
OVERLAY_TYPES = [
    "text", "text", "text", "int2", "int4", "int8", "float8", "text", "bool",
]


def dsn_from_env() -> str:
    """psycopg wants a libpq DSN; compose supplies a SQLAlchemy URL."""
    url = os.environ.get(
        "DATABASE_URL", "postgresql://fieldscope:fieldscope@db:5432/fieldscope"
    )
    return url.replace("postgresql+psycopg://", "postgresql://")


def flush_cache() -> None:
    """Drop cached /area answers, which now describe superseded data."""
    url = os.environ.get("REDIS_URL")
    if not url:
        log("cache: REDIS_URL unset, nothing to flush")
        return
    try:
        import redis

        redis.from_url(url, socket_timeout=2).flushdb()
        log("cache: flushed")
    except Exception as exc:  # a stale cache must not fail an otherwise good load
        log(f"cache: flush failed ({exc}); clear it manually before benchmarking")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def apply_sql_file(conn: psycopg.Connection, path: Path) -> None:
    conn.execute(path.read_text())
    conn.commit()


def load_overlay(conn: psycopg.Connection, parquet: Path) -> int:
    """155,025 rows. Seconds, not minutes -- this is the cheap half."""
    log(f"overlay: reading {parquet.name}")
    dataset = ds.dataset(parquet, format="parquet")

    conn.execute("TRUNCATE overlay")
    started = time.time()
    rows = 0

    cols = ", ".join(OVERLAY_COLUMNS)
    with conn.cursor() as cur:
        with cur.copy(f"COPY overlay ({cols}) FROM STDIN (FORMAT BINARY)") as cp:
            cp.set_types(OVERLAY_TYPES)
            for batch in dataset.to_batches(columns=OVERLAY_COLUMNS, batch_size=20_000):
                columns = [batch.column(c).to_pylist() for c in OVERLAY_COLUMNS]
                for row in zip(*columns):
                    cp.write_row(row)
                rows += batch.num_rows
    conn.commit()

    log(f"overlay: {rows:,} rows in {time.time() - started:.1f}s")
    return rows


def load_soil(conn: psycopg.Connection, parquet: Path, chunk: int,
              simplify: float = 0.0) -> int:
    """The slow one: stream WKB in, reproject to 5070, keep disk flat.

    Each chunk is copied into an unlogged staging table, transformed into
    soil_polygon, then the staging table is truncated -- so peak disk is one
    chunk rather than a second full copy of a 2 GB table.

    `simplify` is a tolerance in metres, applied after the reprojection so the
    unit means something. 30 m leaves 18.8% of the bytes and is the default for
    a deploy, because the crop layer is a 30 m grid: a soil boundary resolved
    more finely than one CDL pixel cannot change an answer this API returns.
    Pass 0 to load the geometry as surveyed (§5.11).
    """
    log(f"soil: reading {parquet.name} (this is the slow step)")
    dataset = ds.dataset(parquet, format="parquet")
    total_rows = dataset.count_rows()

    # Drop the indexes before loading rather than trusting indexes.sql's
    # IF NOT EXISTS to build them afterwards. On a re-run they already exist,
    # so "build the index once at the end" quietly becomes "maintain a GiST
    # index across 1.48M inserts" -- the exact cost this ordering avoids, and
    # invisible in the logs because the later CREATE INDEX then does nothing.
    log("soil: dropping indexes so they are built once, after the load")
    conn.execute("DROP INDEX IF EXISTS soil_polygon_geom_idx")
    conn.execute("DROP INDEX IF EXISTS soil_polygon_mukey_idx")
    conn.commit()

    conn.execute("TRUNCATE soil_polygon")
    conn.execute("DROP TABLE IF EXISTS _soil_raw")
    conn.execute("CREATE UNLOGGED TABLE _soil_raw (mukey text, wkb bytea)")
    conn.commit()

    # Geometry arrives as EPSG:4326 WKB with no SRID attached, so the SRID is
    # set explicitly before transforming -- an unlabelled projection is the
    # classic way to get a join that runs clean and returns nonsense.
    # ST_MakeValid runs only where needed: SSURGO is mostly clean, and an
    # invalid polygon reaching ST_Intersection at request time would fail the
    # request rather than return a wrong answer.
    # Simplification wraps the reprojection rather than replacing it: the
    # tolerance is in metres, so it has to be applied in 5070 and not in
    # degrees. ST_MakeValid still runs afterwards -- SimplifyPreserveTopology
    # keeps a ring from crossing itself but says nothing about the
    # multipolygon it sits in.
    projected = "ST_Transform(ST_SetSRID(ST_GeomFromWKB(wkb), 4326), 5070)"
    if simplify > 0:
        projected = f"ST_SimplifyPreserveTopology({projected}, {simplify})"
        log(f"soil: simplifying to a {simplify:g} m tolerance as it loads")
    else:
        log("soil: loading geometry as surveyed, no simplification")

    transform = f"""
        INSERT INTO soil_polygon (mukey, geom)
        SELECT mukey,
               ST_Multi(
                   CASE WHEN ST_IsValid(g) THEN g
                        ELSE ST_CollectionExtract(ST_MakeValid(g), 3) END
               )
        FROM (
            SELECT mukey, {projected} AS g
            FROM _soil_raw
        ) s
        WHERE NOT ST_IsEmpty(g)
    """

    started = time.time()
    staged = 0
    loaded = 0

    def flush() -> None:
        nonlocal staged, loaded
        if not staged:
            return
        conn.execute(transform)
        conn.execute("TRUNCATE _soil_raw")
        conn.commit()
        loaded += staged
        elapsed = time.time() - started
        pct = 100 * loaded / total_rows
        rate = loaded / elapsed if elapsed else 0
        remaining = (total_rows - loaded) / rate if rate else 0
        log(
            f"soil: {loaded:,}/{total_rows:,} ({pct:.1f}%) "
            f"at {rate:,.0f} rows/s, ~{remaining / 60:.1f} min left"
        )
        staged = 0

    with conn.cursor() as cur:
        for batch in dataset.to_batches(columns=["mukey", "geometry"], batch_size=10_000):
            mukeys = batch.column("mukey").to_pylist()
            wkbs = batch.column("geometry").to_pylist()
            with cur.copy("COPY _soil_raw (mukey, wkb) FROM STDIN (FORMAT BINARY)") as cp:
                cp.set_types(["text", "bytea"])
                for mukey, wkb in zip(mukeys, wkbs):
                    if wkb is not None:
                        cp.write_row((mukey, wkb))
            staged += batch.num_rows
            if staged >= chunk:
                flush()
        flush()

    conn.execute("DROP TABLE IF EXISTS _soil_raw")
    conn.commit()
    log(f"soil: {loaded:,} polygons in {(time.time() - started) / 60:.1f} min")
    return loaded


def build_mukey_area(conn: psycopg.Connection) -> int:
    """Per-map-unit total area -- the denominator POST /area weights by."""
    log("mukey_area: aggregating")
    started = time.time()
    conn.execute("TRUNCATE mukey_area")
    conn.execute(
        """
        INSERT INTO mukey_area (mukey, total_m2, polygons)
        SELECT mukey, SUM(ST_Area(geom)), COUNT(*)
        FROM soil_polygon
        GROUP BY mukey
        """
    )
    conn.commit()
    n = conn.execute("SELECT count(*) FROM mukey_area").fetchone()[0]
    log(f"mukey_area: {n:,} map units in {time.time() - started:.1f}s")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aoi", default="indiana")
    ap.add_argument(
        "--only",
        choices=["all", "overlay", "soil"],
        default="all",
        help="Skip the 2 GB soil load while iterating on the API.",
    )
    ap.add_argument(
        "--chunk",
        type=int,
        default=100_000,
        help="Polygons staged before each transform pass.",
    )
    ap.add_argument(
        "--simplify",
        type=float,
        default=30.0,
        metavar="METRES",
        help="Simplify served geometry to this tolerance; 0 loads it as "
             "surveyed. Default 30, the CDL pixel size (§5.11).",
    )
    args = ap.parse_args()

    overlay_path = DATA / "processed" / f"overlay_{args.aoi}.parquet"
    soil_path = DATA / "raw" / f"ssurgo_{args.aoi}.parquet"

    for path in (overlay_path, soil_path):
        if args.only in ("all", "soil") or path == overlay_path:
            if not path.exists():
                sys.exit(f"missing input: {path}")

    started = time.time()
    with psycopg.connect(dsn_from_env(), autocommit=False) as conn:
        log("applying schema")
        apply_sql_file(conn, SCHEMA_SQL)

        if args.only in ("all", "overlay"):
            conn.execute("DROP INDEX IF EXISTS overlay_mukey_idx")
            conn.commit()
            load_overlay(conn, overlay_path)
        if args.only in ("all", "soil"):
            load_soil(conn, soil_path, args.chunk, args.simplify)
            build_mukey_area(conn)

        log("building indexes (GiST over the soil geometry is the slow one)")
        idx_started = time.time()
        apply_sql_file(conn, INDEXES_SQL)
        log(f"indexes: {(time.time() - idx_started) / 60:.1f} min")

        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("overlay", "soil_polygon", "mukey_area")
        }

    # Invalidate by flushing rather than by expiry (§10 step 4): cached answers
    # describe the data that was loaded when they were computed, and nothing
    # else changes them. A TTL would discard work that is still correct, while
    # leaving genuinely stale answers alive until it happened to fire.
    flush_cache()

    log(f"done in {(time.time() - started) / 60:.1f} min: {counts}")


if __name__ == "__main__":
    main()
