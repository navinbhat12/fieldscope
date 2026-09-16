"""Runtime configuration, read from the environment.

Everything here has a working default so that `docker compose up` needs no
.env file. Anything secret (the database password) is supplied by compose and
never defaulted to something meaningful.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str

    # An upper bound on how much ground one request may ask about. Without it,
    # a polygon drawn around the whole state would aggregate every map unit in
    # the table -- slow, and not a question the demo is trying to answer. The
    # cap is generous next to a field: 100,000 acres is roughly 156 square
    # miles, where a large Indiana farm field is about 80.
    max_query_acres: float = 100_000.0

    # Decimal places retained when normalising a polygon for the cache key
    # (§10 step 4). Six places is ~0.1 m at these latitudes -- far finer than
    # anyone draws by hand, so two attempts at the same field still collide,
    # while genuinely different fields never do. Unused until step 4; defined
    # here so the API and the future cache agree on one value.
    coord_precision: int = 6

    # Switched off by the benchmark to measure the uncached path against the
    # same running service, rather than inferring it from a cold cache.
    cache_enabled: bool = True

    # Exposed so the benchmark can test whether the p95 tail is query cost or
    # simply requests queueing for a connection. Small by default: each
    # Postgres backend costs memory, and the deploy target has ~1 GB for the
    # database, the cache and this service together.
    db_pool_size: int = 5

    # Browsers refuse a cross-origin fetch that the server does not opt into,
    # and the frontend is served from a different host than this API. Defaults
    # to "*" because every endpoint is read-only, unauthenticated and public --
    # there is no session for another origin to ride. Narrow it by setting
    # CORS_ORIGINS if that ever stops being true.
    cors_origins: tuple[str, ...] = ("*",)

    # An upper bound on how many map units POST /area/mapunits will return
    # geometry for. The acreage cap above bounds the *area* of a request but
    # not the number of shapes inside it, and geometry is far more expensive to
    # serialise than a number. A field touches a handful of map units; 200 is
    # well past that, and past it the map would be unreadable anyway.
    max_geometry_features: int = 200


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://fieldscope:fieldscope@localhost:5432/fieldscope",
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        max_query_acres=float(os.environ.get("MAX_QUERY_ACRES", "100000")),
        coord_precision=int(os.environ.get("COORD_PRECISION", "6")),
        cache_enabled=os.environ.get("CACHE_ENABLED", "1") not in ("0", "false", "False"),
        db_pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
        cors_origins=tuple(
            o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
        ),
        max_geometry_features=int(os.environ.get("MAX_GEOMETRY_FEATURES", "200")),
    )
