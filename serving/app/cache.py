"""Read-through cache for POST /area (docs/DESIGN.md §5.7, §10 step 4).

Only /area is cached. GET /mapunit is an indexed lookup on 155,025 rows and
Postgres already answers it in well under a millisecond; putting Redis in front
of that would be decoration. /area is different: it does a spatial lookup and
an aggregation across every map unit the drawn polygon touches, and the query
space is unbounded because a caller can draw anything.

Two decisions worth stating.

**The key is a hash of the normalised polygon, and the normalised polygon is
also what gets queried.** Rounding coordinates for the key but computing the
answer from the raw polygon would mean one key could legitimately map to
several different answers. Rounding once, up front, keeps key and answer in
agreement by construction.

**No TTL.** The underlying data changes only when the batch pipeline reruns, so
expiry would only throw away work that is still correct. Invalidation is a
flush at the end of a load instead.
"""

import hashlib
import json
import logging
from typing import Any

import redis

log = logging.getLogger("fieldscope.cache")

# Bump when the response shape or the aggregation changes, so that old entries
# are ignored rather than served. Cheaper and safer than remembering to flush.
SCHEMA_VERSION = "v1"


def normalise(geometry: dict, precision: int) -> dict:
    """Round every coordinate so near-identical drawings collide.

    A hand-drawn field never reproduces to the last decimal, and unrounded
    coordinates would make the cache useless -- every redraw a miss. At the
    default of 6 decimal places the grid is roughly 0.1 m, far finer than
    anyone draws and far coarser than the jitter between two attempts at the
    same boundary.

    What this does *not* do is canonicalise vertex order or winding: the same
    ring starting at a different corner hashes differently and misses. Fixing
    that is possible but buys little, since a client redrawing a field produces
    new vertices rather than a rotation of the old ones.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, (int, float)):
            return round(float(node), precision)
        return [walk(child) for child in node]

    return {"type": geometry["type"], "coordinates": walk(geometry["coordinates"])}


def key_for(geometry: dict) -> str:
    """A stable key for an already-normalised geometry."""
    blob = json.dumps(geometry, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode()).hexdigest()[:32]
    return f"area:{SCHEMA_VERSION}:{digest}"


class Cache:
    """Redis wrapper that degrades to a no-op rather than failing a request.

    A cache outage should cost latency, not availability: every operation is
    wrapped, and a failure is logged once and treated as a miss. The API stays
    correct with Redis stopped entirely -- which is also how the benchmark
    measures the uncached path.
    """

    def __init__(self, url: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self._client = redis.from_url(url, socket_timeout=0.25) if enabled else None

    def get(self, key: str) -> dict | None:
        if not self._client:
            return None
        try:
            raw = self._client.get(key)
        except redis.RedisError as exc:
            log.warning("cache get failed, treating as miss: %s", exc)
            return None
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict) -> None:
        if not self._client:
            return
        try:
            self._client.set(key, json.dumps(value))
        except redis.RedisError as exc:
            log.warning("cache set failed, continuing uncached: %s", exc)

    def flush(self) -> None:
        """Drop every cached answer. Called after a load, per §10 step 4."""
        if not self._client:
            return
        try:
            self._client.flushdb()
        except redis.RedisError as exc:
            log.warning("cache flush failed: %s", exc)

    def stats(self) -> dict:
        if not self._client:
            return {"enabled": False}
        try:
            info = self._client.info("stats")
            hits = info.get("keyspace_hits", 0)
            misses = info.get("keyspace_misses", 0)
            total = hits + misses
            return {
                "enabled": True,
                "keys": self._client.dbsize(),
                "keyspace_hits": hits,
                "keyspace_misses": misses,
                "hit_rate": round(hits / total, 4) if total else None,
            }
        except redis.RedisError as exc:
            return {"enabled": True, "error": str(exc)}
