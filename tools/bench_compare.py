#!/usr/bin/env python3
"""
Compares benchmark runs recorded by tools/bench.py in
benchmarks/results.jsonl. Only runs sharing the same --tag are compared -
cross-machine numbers (laptop vs NAS vs a fake test container) are not
meaningful, see docs/testing-guide.md.

Default: for each (tag, phase) pair present in the log, show the oldest
and newest run and flag any metric that regressed beyond --threshold.

    python3 tools/bench_compare.py
    python3 tools/bench_compare.py --tag bob-laptop-26k
    python3 tools/bench_compare.py --tag bob-laptop-26k --commit abc1234
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = REPO_ROOT / "benchmarks" / "results.jsonl"

# Metric name -> True if higher is better (so a regression is a decrease),
# False if lower is better (a regression is an increase).
_HIGHER_IS_BETTER = {
    "files_per_s": True,
    "elapsed_s": False,
    "wall_s": False,
    "peak_rss_mb": False,
    "peak_child_rss_mb": False,
}


def load_results():
    if not RESULTS_PATH.is_file():
        return []
    with RESULTS_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def regressed(metric, old, new, threshold):
    if metric not in _HIGHER_IS_BETTER or old in (None, 0):
        return False
    change = (new - old) / abs(old)
    if _HIGHER_IS_BETTER[metric]:
        return change < -threshold
    return change > threshold


def print_comparison(tag, phase, old, new, threshold):
    print(f"\n{tag} / {phase}")
    print(f"  {old['git_commit']} ({old['timestamp']})  ->  {new['git_commit']} ({new['timestamp']})")
    flagged = []
    for metric in sorted(_HIGHER_IS_BETTER):
        if metric not in old or metric not in new:
            continue
        old_v, new_v = old[metric], new[metric]
        pct_str = f"{(new_v - old_v) / abs(old_v) * 100:+.1f}%" if old_v else "n/a"
        marker = " ⚠ REGRESSION" if regressed(metric, old_v, new_v, threshold) else ""
        if marker:
            flagged.append(metric)
        print(f"    {metric:20s} {old_v:>10}  ->  {new_v:>10}  ({pct_str}){marker}")
    return flagged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", help="only compare this machine/library tag")
    ap.add_argument("--threshold", type=float, default=0.10, help="fraction change to flag as a regression (default 0.10 = 10%%)")
    ap.add_argument("--commit", help="compare the newest run against the newest run at/before this commit, instead of oldest-vs-newest")
    args = ap.parse_args()

    results = load_results()
    if args.tag:
        results = [r for r in results if r["tag"] == args.tag]
    if not results:
        print("no benchmark results found (run tools/bench.py first)" + (f" for tag {args.tag!r}" if args.tag else ""))
        return

    by_group = defaultdict(list)
    for r in results:
        by_group[(r["tag"], r["phase"])].append(r)

    any_regression = False
    for (tag, phase), runs in sorted(by_group.items()):
        runs.sort(key=lambda r: r["timestamp"])
        if len(runs) < 2:
            print(f"\n{tag} / {phase}: only one run recorded, nothing to compare yet")
            continue
        if args.commit:
            baseline_candidates = [r for r in runs if r["git_commit"] == args.commit]
            old = baseline_candidates[-1] if baseline_candidates else runs[0]
        else:
            old = runs[0]
        new = runs[-1]
        flagged = print_comparison(tag, phase, old, new, args.threshold)
        any_regression = any_regression or bool(flagged)

    if any_regression:
        print("\nRegressions found - see ⚠ above. Not a hard failure; use judgement (a noisy "
              "single run, a deliberate tradeoff, a one-off system load spike).")


if __name__ == "__main__":
    main()
