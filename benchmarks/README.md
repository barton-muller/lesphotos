# Benchmark results

`results.jsonl` is the append-only log `tools/bench.py` writes to, one JSON object
per line, one line per (phase, run). It's git-tracked deliberately - the commit
history of this file **is** the performance log, so a regression can be bisected
the same way as any other bug.

Run `python3 tools/bench.py --root <library> --tag <machine-tag>` to add entries,
and `python3 tools/bench_compare.py` to compare them. See `docs/testing-guide.md`
for the full workflow, including why cross-machine numbers are never comparable
and what `--tag` should be for a given environment.

Don't hand-edit `results.jsonl`. Don't delete old entries to "clean up" - a run
from six months ago on the same `--tag` is exactly what makes a slow regression
visible.
