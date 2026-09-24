# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning is
described in `docs/decisions/versioning.md`. Entries land in the same commit that bumps
`VERSION` - see that doc for when a bump is warranted.

## [Unreleased]

### Added
- `VERSION` file and `/health`'s `version` field, so a NAS restart can be confirmed as
  running the intended build (shown in the client's profile menu).
- `server/tests/` - a pytest suite for pure logic and security-critical path validation
  (`moment_dates`, `fakelib/grouping`, `api._resolve_host_path`, `auth` password hashing).
  Complements, not replaces, the real-app validation `CLAUDE.md` already describes.
- `client/web/tests/` - a committed `@playwright/test` suite (desktop + real Pixel 7 touch
  device) against a fully isolated, self-bootstrapping instance, covering the Android UI
  fixes below as maintained regression checks rather than one-off scripts.
- `tools/bench.py` / `tools/bench_compare.py` - indexer performance benchmarking against a
  real library, logged to `benchmarks/results.jsonl` and comparable across commits (only
  within the same `--tag`, i.e. the same machine/library).
- `docs/testing-guide.md` - the fuller testing guide for agents working on this repo.

### Fixed
- Favoriting a photo no longer blanks and rescrolls the grid to the top.
- Pinch-to-zoom density on touch: a pinch previously stalled after one step and could be
  hijacked by the browser's native page-zoom.
- The density +/- buttons no longer push Select/Merge behind a horizontal scrollbar on
  narrow (phone) layouts - hidden there in favor of pinch.
- The share button no longer shows a status message after a successful native share.
- "Download Thumbnails" shortened to an icon + "Thumbs" in the profile menu.

## [0.1.0] - unreleased baseline

Everything before this changelog existed. Not reconstructed retroactively - see `git log`
for the real history. This entry exists so `[Unreleased]` has something to diff against.
