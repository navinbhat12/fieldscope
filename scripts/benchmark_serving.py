"""Benchmark POST /area under stated synthetic load (docs/DESIGN.md §5.7, §10 step 5).

**This measures a benchmark, not production traffic.** Fieldscope has no users;
any latency figure here describes a load generator driving a local container,
and must be reported that way. A number presented as production behaviour would
be false (§7).

Method, so a reader can reproduce or dispute it:

- A fixed, committed set of 60 field-sized polygons (scripts/bench_polygons.json),
  in five size classes, centred on real Indiana map units. The cost of /area
  scales with how many map units a polygon touches, so a single polygon size
  would measure one shape of query rather than the endpoint.
- Open-loop load: requests are issued on a fixed schedule at a target rate,
  not one-after-another. Closed-loop sending would let a slow server slow the
  offered load and hide exactly the queueing the rate is meant to expose. The
  achieved rate is reported alongside the target; if they diverge, the target
  was not met and the latency figures should be read in that light.
- Three phases, each measuring a different path through the same service:
    baseline  POST /ping, which does no work. The floor: HTTP, ASGI, Pydantic
              validation and Docker's networking. Everything below includes it,
              so it is what must be subtracted before claiming what caching saved.
    uncached  API restarted with CACHE_ENABLED=0. Every request reaches PostGIS.
    cold      Cache on but flushed. Every request is a miss that populates it.
    warm      Cache pre-populated. Every request is a hit. This is a CEILING,
              not a result: no real workload hits on every request, and p95 is
              exactly the percentile where the misses live.
    mixed     A stated fraction of requests drawn from a pre-warmed subset, the
              rest from polygons never seen. The honest middle, and the one
              worth quoting -- at a 50% hit rate a p95 is still decided by
              misses, because one request in twenty being slow is what p95
              measures.
- N runs per phase, the first discarded as warm-up (JVM-free here, but page
  cache, connection pools and Postgres plan caches all settle), then the median
  of the per-run medians is reported with the full observed range. A single
  sample from a distribution with this much spread is not a defensible claim.

Usage:
    python scripts/benchmark_serving.py --phase warm --rate 50 --requests 600
"""

import argparse
import json
import platform
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
POLYGONS = ROOT / "scripts" / "bench_polygons.json"


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Explicit so the number is unambiguous."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[idx]


def hardware() -> dict:
    """Capture what the numbers depend on -- required alongside any figure."""
    def sysctl(key: str) -> str:
        try:
            return subprocess.run(
                ["sysctl", "-n", key], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:
            return "unknown"

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": sysctl("machdep.cpu.brand_string"),
        "cores": sysctl("hw.ncpu"),
        "memory_gb": round(int(sysctl("hw.memsize") or 0) / 1e9, 1),
        "python": platform.python_version(),
    }


def one_request(session: requests.Session, url: str, body: dict) -> tuple[float, int, bool]:
    started = time.perf_counter()
    try:
        r = session.post(url, json=body, timeout=30)
        elapsed = (time.perf_counter() - started) * 1000
        cached = bool(r.json().get("cached")) if r.status_code == 200 else False
        return elapsed, r.status_code, cached
    except Exception:
        return (time.perf_counter() - started) * 1000, 0, False


def flush_cache(root: Path) -> None:
    subprocess.run(
        ["docker", "compose", "exec", "-T", "cache", "redis-cli", "FLUSHDB"],
        cwd=root, capture_output=True, timeout=30,
    )


def run_once(url: str, bodies: list[dict], rate: float, n: int) -> dict:
    """One run: n requests issued on a fixed schedule at `rate` per second."""
    workers = max(8, int(rate * 0.5))
    results: list[tuple[float, int, bool]] = []

    with requests.Session() as session, ThreadPoolExecutor(max_workers=workers) as pool:
        started = time.perf_counter()
        futures = []
        for i in range(n):
            due = started + i / rate
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            futures.append(pool.submit(one_request, session, url, bodies[i % len(bodies)]))
        for f in futures:
            results.append(f.result())
        wall = time.perf_counter() - started

    latencies = [r[0] for r in results if r[1] == 200]
    errors = [r[1] for r in results if r[1] != 200]
    hits = sum(1 for r in results if r[2])

    return {
        "requests": n,
        "ok": len(latencies),
        "errors": len(errors),
        "error_codes": sorted(set(errors)),
        "wall_s": round(wall, 3),
        "achieved_rate": round(len(results) / wall, 1) if wall else 0,
        "p50_ms": round(statistics.median(latencies), 2) if latencies else None,
        "p95_ms": round(percentile(latencies, 95), 2) if latencies else None,
        "p99_ms": round(percentile(latencies, 99), 2) if latencies else None,
        "min_ms": round(min(latencies), 2) if latencies else None,
        "max_ms": round(max(latencies), 2) if latencies else None,
        "cache_hits": hits,
        "hit_rate": round(hits / len(results), 4) if results else 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8000/area")
    ap.add_argument(
        "--phase", choices=["baseline", "uncached", "cold", "warm", "mixed"], required=True,
        help="baseline drives POST /ping -- the floor to subtract from the rest.",
    )
    ap.add_argument(
        "--hit-rate", type=float, default=0.5,
        help="Target hit rate for --phase mixed (default 0.5).",
    )
    ap.add_argument("--rate", type=float, default=50.0, help="Target requests/second.")
    ap.add_argument("--requests", type=int, default=600)
    ap.add_argument("--runs", type=int, default=5, help="First is discarded as warm-up.")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if args.phase == "baseline" and args.url.endswith("/area"):
        args.url = args.url[: -len("/area")] + "/ping"

    spec = json.loads(POLYGONS.read_text())
    bodies = [{"geometry": p["geometry"]} for p in spec["polygons"]]

    n = args.requests

    print(f"phase={args.phase}  target={args.rate}/s  n={n}  runs={args.runs} "
          f"(first discarded)\n")

    runs = []
    bodies_for_run = bodies
    for i in range(args.runs):
        if args.phase == "mixed":
            # Warm a prefix of the set, leave the rest untouched, then draw
            # from each according to the target hit rate. Deterministic split
            # rather than random, so a run is reproducible.
            flush_cache(ROOT)
            warm_n = max(1, int(len(bodies) * args.hit_rate))
            run_once(args.url, bodies[:warm_n], args.rate, warm_n)
            picks = []
            for k in range(n):
                if (k % 100) < int(args.hit_rate * 100):
                    picks.append(bodies[k % warm_n])
                else:
                    picks.append(bodies[warm_n + (k % max(1, len(bodies) - warm_n))])
            bodies_for_run = picks
        elif args.phase == "cold":
            # One flush per run, outside the timed loop. The polygon set is
            # larger than a run's request count, so every request is still a
            # first touch without flushing mid-run -- which would mean a
            # ~300ms docker exec inside the pacing loop, destroying the very
            # schedule the open-loop design exists to hold.
            flush_cache(ROOT)
        elif args.phase == "warm" and i == 0:
            # Populate before measuring, so run 1 is not a disguised cold run.
            run_once(args.url, bodies, args.rate, len(bodies))

        r = run_once(
            args.url,
            bodies_for_run if args.phase == "mixed" else bodies,
            args.rate, n,
        )
        runs.append(r)
        label = "warm-up, discarded" if i == 0 else f"run {i}"
        print(f"  {label:<20} p50={r['p50_ms']:>7} ms  p95={r['p95_ms']:>7} ms  "
              f"achieved={r['achieved_rate']:>5}/s  hit_rate={r['hit_rate']}"
              + (f"  ERRORS {r['error_codes']}" if r["errors"] else ""))

    kept = runs[1:] or runs
    p50s = [r["p50_ms"] for r in kept if r["p50_ms"] is not None]
    p95s = [r["p95_ms"] for r in kept if r["p95_ms"] is not None]

    summary = {
        "phase": args.phase,
        "target_rate": args.rate,
        "requests_per_run": n,
        "runs_kept": len(kept),
        "p50_median_ms": round(statistics.median(p50s), 2) if p50s else None,
        "p50_range_ms": [min(p50s), max(p50s)] if p50s else None,
        "p95_median_ms": round(statistics.median(p95s), 2) if p95s else None,
        "p95_range_ms": [min(p95s), max(p95s)] if p95s else None,
        "achieved_rate_median": statistics.median([r["achieved_rate"] for r in kept]),
        "hit_rate_median": statistics.median([r["hit_rate"] for r in kept]),
        "total_errors": sum(r["errors"] for r in kept),
        "hardware": hardware(),
        "runs": runs,
        "note": (
            "Synthetic load against a local container. Not production traffic; "
            "this service has no users."
        ),
    }

    print(f"\n  {args.phase}: p50 {summary['p50_median_ms']} ms "
          f"(range {summary['p50_range_ms']}), p95 {summary['p95_median_ms']} ms "
          f"(range {summary['p95_range_ms']})")
    print(f"  achieved {summary['achieved_rate_median']}/s of {args.rate}/s target, "
          f"hit rate {summary['hit_rate_median']}, errors {summary['total_errors']}")

    if args.out:
        args.out.write_text(json.dumps(summary, indent=2))
        print(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
