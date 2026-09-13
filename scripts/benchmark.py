"""Benchmark the join over repeated runs and report a defensible number.

A single timing of this pipeline means very little. Identical code over
identical data has been observed anywhere between 14.6s and 54.3s on the same
machine, driven by JVM warmup, OS page cache state, and whatever else the
machine is doing. Correctness numbers are exactly reproducible; wall time is
not.

So every performance claim in this repository comes from here: N runs, the
cold first run discarded, median and full range reported alongside the sample
size and the hardware. Outliers are surfaced rather than silently dropped --
one run during development stalled at 614.7s while the machine slept, bracketed
by normal runs either side, and a method that quietly discards that is a method
that can quietly discard anything.

    .venv/bin/python -u scripts/benchmark.py --runs 7
"""

import argparse
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOIN = ROOT / "scripts" / "run_join.py"

# Anything slower than this is a stalled machine, not a slow pipeline. Reported
# separately and excluded from the median rather than dropped on the floor.
OUTLIER_SECS = 100.0


def one_run() -> tuple[float, int, float]:
    """Run the join once; return (join wall seconds, records joined, total seconds)."""
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-u", str(JOIN)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,  # return code is inspected below, with the tail of the log
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:], file=sys.stderr)
        raise SystemExit(f"join failed (exit {proc.returncode})")

    secs = re.search(r"join wall time:\s+([\d.]+)s", proc.stdout)
    recs = re.search(r"land cover records:\s+([\d,]+)", proc.stdout)
    if not secs or not recs:
        raise SystemExit("could not parse join output -- did the log format change?")
    return float(secs.group(1)), int(recs.group(1).replace(",", "")), time.time() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--csv", type=Path, default=None, help="write per-run timings here")
    args = ap.parse_args()

    print(f"benchmarking {args.runs} runs\n")
    rows = []
    for i in range(1, args.runs + 1):
        join_s, n_recs, total_s = one_run()
        rows.append((i, join_s, n_recs))
        flag = "  <- outlier, excluded" if join_s >= OUTLIER_SECS else ""
        print(f"  run {i}: join {join_s:6.1f}s   total {total_s:6.1f}s   "
              f"{n_recs / join_s:>9,.0f} rec/s{flag}", flush=True)

    valid = [s for _, s, _ in rows if s < OUTLIER_SECS]
    excluded = len(rows) - len(valid)
    if not valid:
        raise SystemExit("every run was an outlier -- machine is not in a fit state to benchmark")

    n_recs = rows[0][2]
    med = statistics.median(valid)
    print(f"\n{'-' * 60}")
    print(f"records         {n_recs:,}")
    print(f"valid runs      {len(valid)} of {len(rows)}" + (f" ({excluded} excluded)" if excluded else ""))
    print(f"join wall time  median {med:.1f}s   range {min(valid):.1f}-{max(valid):.1f}s")
    print(f"throughput      median {n_recs / med:,.0f} records/sec")
    if len(valid) > 1:
        print(f"stdev           {statistics.stdev(valid):.1f}s")

    if args.csv:
        args.csv.write_text(
            "run,join_secs,records,excluded\n"
            + "".join(f"{i},{s},{n},{int(s >= OUTLIER_SECS)}\n" for i, s, n in rows)
        )
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
