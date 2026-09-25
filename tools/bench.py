#!/usr/bin/env python3
"""
Runs the indexer against a real library and records timing/memory so
performance can be tracked and compared across commits - see
docs/testing-guide.md's Benchmarking section for how to use this.

Each phase runs indexer.py as a *fresh subprocess*, not an in-process
import: peak_rss (resource.getrusage) is a cumulative high-water mark for
the whole process, so measuring two phases in one process would have the
second phase's number include the first's. A subprocess per phase gives
each an isolated, honest peak.

Two phases, both non-destructive by default (safe to run against a real
library, including a 20,000+ photo one, without touching originals or
forcing a slow full rebuild):

  incremental_scan  - indexer.py --no-derive against an already-indexed
                       library. Measures walk+skip-check throughput, not
                       decode cost - what dominates day-to-day "did my
                       change slow down every startup" runs.
  derive_pending     - indexer.py --derive-pending --limit N. Measures
                       actual decode+thumbnail cost per photo. Only
                       processes photos with no derivative yet, so by
                       default this does almost nothing on an
                       already-fully-derived library - pass --undo-derive
                       to manufacture pending work first (see below), or
                       point --root at a library that still has some.

Results append as one JSON object per line to benchmarks/results.jsonl
(git-tracked, so the history itself is the log). Compare with
tools/bench_compare.py.

CROSS-MACHINE NUMBERS ARE NOT COMPARABLE. A 26,000-photo laptop run and a
DS124 NAS run measure different hardware entirely - only compare runs that
share the same --tag. See docs/testing-guide.md.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = REPO_ROOT / "benchmarks" / "results.jsonl"

# elapsed=1.23s  4.5 files/s
_ELAPSED_RE = None


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _current_version():
    version_file = REPO_ROOT / "VERSION"
    return version_file.read_text().strip() if version_file.is_file() else "unknown"


def _run_indexer(args, extra):
    cmd = [sys.executable, "indexer.py", *extra]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT / "server", capture_output=True, text=True,
    )
    wall_s = round(time.monotonic() - t0, 2)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"indexer.py {' '.join(extra)} failed (exit {proc.returncode})")
    return proc.stderr, wall_s


def _parse_index_stats(stderr):
    # Matches the summary indexer.py's main() prints after a normal
    # (non --derive-pending, non --status) run - see indexer.py's own
    # print() calls at the end of main().
    stats = {}
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("seen="):
            for part in line.split():
                k, _, v = part.partition("=")
                stats[k] = int(v)
        elif line.startswith("elapsed=") and "files/s" in line:
            elapsed, _, rest = line.partition("  ")
            stats["elapsed_s"] = float(elapsed.removeprefix("elapsed=").removesuffix("s"))
            stats["files_per_s"] = float(rest.split()[0])
        elif line.startswith("peak_rss="):
            parts = line.split()
            stats["peak_rss_mb"] = float(parts[0].removeprefix("peak_rss=").removesuffix("MB"))
    return stats


def _parse_derive_pending_stats(stderr):
    # derived=N failed=N remaining=N  elapsed=Ns
    stats = {}
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("derived="):
            fields, _, elapsed_part = line.partition("  elapsed=")
            for part in fields.split():
                k, _, v = part.partition("=")
                stats[k] = int(v)
            stats["elapsed_s"] = float(elapsed_part.removesuffix("s"))
    return stats


def _status(root_args):
    proc = subprocess.run(
        [sys.executable, "indexer.py", *root_args, "--status"],
        cwd=REPO_ROOT / "server", capture_output=True, text=True, check=True,
    )
    registered = None
    for line in proc.stdout.splitlines():
        if line.startswith("registered:"):
            registered = int(line.split(":", 1)[1].strip())
    return registered


def record(entry):
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    print(json.dumps(entry, indent=2, sort_keys=True))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root_group = ap.add_mutually_exclusive_group(required=True)
    root_group.add_argument("--root", type=Path, help="library folder to benchmark against")
    root_group.add_argument("--user", help="benchmark this account's library instead of --root")
    ap.add_argument(
        "--tag", required=True,
        help="machine/library identity, e.g. bob-laptop-26k, nas-nas, "
             "container-fake-1k. Only runs sharing a --tag are comparable.",
    )
    ap.add_argument(
        "--derive-limit", type=int, default=200,
        help="max photos for the derive_pending phase (default 200 - "
             "enough to be a stable sample, not so many that a library "
             "with lots of pending work makes every benchmark run slow)",
    )
    ap.add_argument(
        "--skip-derive", action="store_true",
        help="only run the incremental_scan phase (skip derive_pending "
             "entirely - useful when the library has no pending photos "
             "and you don't want to manufacture any)",
    )
    args = ap.parse_args()

    # Absolute: indexer.py subprocesses run with cwd=server/, so a relative
    # --root here would resolve against the wrong directory.
    root_args = ["--user", args.user] if args.user else ["--root", str(args.root.resolve())]
    commit = _git("rev-parse", "--short", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    base_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": commit,
        "dirty": dirty,
        "version": _current_version(),
        "tag": args.tag,
        "library_size_photos": _status(root_args),
    }

    print(f"== incremental_scan ({args.tag}) ==", file=sys.stderr)
    stderr, wall_s = _run_indexer(args, [*root_args, "--no-derive"])
    stats = _parse_index_stats(stderr)
    record({**base_entry, "phase": "incremental_scan", "wall_s": wall_s, **stats})

    if not args.skip_derive:
        print(f"== derive_pending ({args.tag}, limit={args.derive_limit}) ==", file=sys.stderr)
        stderr, wall_s = _run_indexer(
            args, [*root_args, "--derive-pending", "--limit", str(args.derive_limit)]
        )
        stats = _parse_derive_pending_stats(stderr)
        record({**base_entry, "phase": "derive_pending", "wall_s": wall_s, **stats})


if __name__ == "__main__":
    main()
