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


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://fieldscope:fieldscope@localhost:5432/fieldscope",
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        max_query_acres=float(os.environ.get("MAX_QUERY_ACRES", 100_000)),
        coord_precision=int(os.environ.get("COORD_PRECISION", 6)),
    )
