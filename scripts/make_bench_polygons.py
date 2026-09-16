"""Generate the fixed polygon set the serving benchmark drives requests with.

The original set was produced ad hoc and committed as JSON with no way to
regenerate it. That went unnoticed until the AOI changed: the polygons were
centred on Indiana map units, so against a California database all 300 returned
an empty answer -- and a benchmark over empty answers reports excellent latency
for doing no work. The set has to follow the AOI, so it needs a script.

Polygons are squares centred on randomly sampled map units that actually have
overlay coverage, in five size classes, so the benchmark spans a realistic range
of map-unit counts rather than one shape of query. The seed is fixed, so a given
AOI always produces the same set and runs stay comparable across machines and
dates.

Reads the loaded serving database rather than the Parquet, because "has overlay
coverage" is exactly the condition the API answers against.

    docker compose run --rm api python scripts/make_bench_polygons.py --aoi california
"""

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]

# Field-sized, in metres from centre to edge. A quarter-section is ~400 m a
# side, so this brackets a real field an order of magnitude either way and the
# largest class is what produces the multi-map-unit queries that dominate the
# tail (DESIGN.md section 11).
SIZE_CLASSES = [100, 200, 400, 800, 1600]

SEED = 20260915


def dsn_from_env() -> str:
    import os

    url = os.environ.get("DATABASE_URL", "")
    # SQLAlchemy-style URL in the container env; psycopg wants the plain form.
    return url.replace("postgresql+psycopg://", "postgresql://") or (
        "postgresql://fieldscope:fieldscope@db:5432/fieldscope"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aoi", default="california")
    ap.add_argument("--count", type=int, default=300,
                    help="Larger than a run's request count, so a cold run never "
                         "repeats a polygon and every request is a genuine miss.")
    ap.add_argument("--out", default=None, help="Path, or - for stdout.")
    args = ap.parse_args()

    out = args.out or (ROOT / "scripts" / f"bench_polygons_{args.aoi}.json")
    rng = random.Random(SEED)

    with psycopg.connect(dsn_from_env()) as conn:
        # Only map units the overlay actually knows about: a polygon centred on
        # a mukey with no overlay row would measure the empty path, which is the
        # failure this script exists to prevent.
        rows = conn.execute(
            """
            SELECT ST_X(ST_Centroid(ST_Transform(sp.geom, 4326))) AS lon,
                   ST_Y(ST_Centroid(ST_Transform(sp.geom, 4326))) AS lat
            FROM soil_polygon sp
            WHERE sp.mukey IN (SELECT DISTINCT mukey FROM overlay)
              AND ST_Area(sp.geom) > 100000
            ORDER BY md5(sp.ctid::text || sp.mukey)
            LIMIT %s
            """,
            (args.count,),
        ).fetchall()

    if len(rows) < args.count:
        sys.exit(f"only {len(rows)} candidate map units; need {args.count}")

    polygons = []
    for i, (lon, lat) in enumerate(rows):
        half = SIZE_CLASSES[i % len(SIZE_CLASSES)]
        # Degrees per metre at this latitude. Good enough for a benchmark
        # fixture: the polygon only has to be field-sized and in the right
        # place, and every acreage it produces is computed in 5070 anyway.
        dlat = half / 111_320.0
        dlon = half / (111_320.0 * max(0.1, abs(__import__("math").cos(__import__("math").radians(lat)))))
        ring = [
            [round(lon - dlon, 6), round(lat - dlat, 6)],
            [round(lon + dlon, 6), round(lat - dlat, 6)],
            [round(lon + dlon, 6), round(lat + dlat, 6)],
            [round(lon - dlon, 6), round(lat + dlat, 6)],
            [round(lon - dlon, 6), round(lat - dlat, 6)],
        ]
        polygons.append({
            "id": f"p{i:03d}",
            "half_side_m": half,
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })

    rng.shuffle(polygons)

    doc = {
        "description": (
            f"Fixed set of field-sized polygons for benchmarking POST /area "
            f"(docs/DESIGN.md section 5.7). Centred on randomly sampled {args.aoi} soil "
            f"map units that have overlay coverage, in five size classes so the "
            f"benchmark spans a realistic range of map-unit counts rather than one "
            f"shape of query. The set is larger than a benchmark run's request count "
            f"so that every request in a cold run is a genuine first touch, without "
            f"flushing inside the timed loop. Regenerate with "
            f"scripts/make_bench_polygons.py --aoi {args.aoi}."
        ),
        "aoi": args.aoi,
        "generated": date.today().isoformat(),
        "seed": SEED,
        "count": len(polygons),
        "polygons": polygons,
    }
    # Status goes to stderr so `--out -` can stream clean JSON to a pipe. The
    # container mounts scripts/ read-only, so writing in place is not available
    # when this runs where the database is reachable.
    if str(out) == "-":
        json.dump(doc, sys.stdout, indent=1)
    else:
        out.write_text(json.dumps(doc, indent=1))
    print(f"wrote {len(polygons)} polygons over {args.aoi}", file=sys.stderr)
    print(f"  size classes (half-side m): {SIZE_CLASSES}", file=sys.stderr)


if __name__ == "__main__":
    main()
