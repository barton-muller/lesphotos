# Testing guide (for agents)

This extends `CLAUDE.md`'s "How validation is done" section - read that first, this doesn't
repeat it. The short version of the philosophy there still holds and isn't changed by
anything below: **there is no unit suite that replaces driving the real app.** What's below
adds three things that were missing - a documented two-tier testing setup, a small
automated suite for logic that's cheap to check without a browser, and a way to actually
track whether performance is getting better or worse over time.

## Two tiers of library

**Tier 1 - every session, always available.** A fakelib-generated library. No real photos
required, works in a fresh container with nothing mounted. Use it for functional
correctness and for exercising UI at a real *date-range* scale (years of moments), not real
*volume* scale:

```sh
cd server
python3 fakelib/generate_history_backfill.py --user qa --moments-per-year 4 --photos-per-moment 12
python3 manage.py add-user qa "$PWD/data/photos/qa" --password <something>
python3 indexer.py --user qa
```

This is what an earlier session used, via ad hoc Playwright scripts, to validate the Android
UI fixes (pinch, favorite refresh, etc.) in commit `7098ad5` - those checks are now the
committed `@playwright/test` suite under `client/web/tests/`, see "Committed end-to-end
tests" below.

**Tier 2 - Bob's laptop, ~26,000 real photos.** This is the primary place real testing
happens, and the only place that can validate anything volume-dependent: Safari's per-row
IndexedDB cost (`timeline-load-and-safari-perf.md`), real HEIC/video derive timing, scroll
performance with the real virtualizer at real density, sync/download-all across a library
that large. **An agent without access to this library must say so explicitly** rather than
imply volume was validated - "verified in a 1,152-photo fake library" and "verified against
the 26,000-photo library" are different claims and should be reported as such. A cloud
session almost never has Tier 2 access; say so up front rather than after the fact.

If you *do* have access to the 26k library (i.e. you're running locally on that laptop),
prefer it over generating a fake one for anything that could plausibly be volume-sensitive -
which in this codebase is most things, per `CLAUDE.md`'s own recurring theme ("this project
has a consistent history of otherwise-correct-looking code failing at real library size").

## What to actually test

Pull from `CLAUDE.md`'s "Gotchas worth not rediscovering" - that list exists because each
item broke silently once already. Before shipping a change, check whether it touches any of
those areas and if so, re-verify the specific gotcha, not just "does it look right":

- **Auth/session**: cold start (see below), multi-user isolation (two real accounts, cross-
  request by exact `photo_id`), cookie behavior after a restart.
- **Timeline**: `/timeline` vs `/timeline/count` filter parity, scroll position across a
  starredOnly round-trip, scroll position surviving a background write (the favorite-toggle
  bug just fixed), density change via both pinch and the +/- buttons, cold-start count-match
  skipping a relist correctly.
- **Sync**: incremental (`since=`) picks up a rename (not just new photos - the "same
  photo_id, different metadata" case `sync.ts` exists to handle), a full resync recovers
  from a bad local state, a bulk thumbnail download is resumable after being interrupted
  mid-way (kill the tab, reload, confirm it picks up rather than restarting).
- **Offline/PWA**: `npm run build && npm run preview`, not `vite dev` (`vite-plugin-pwa`'s
  service worker doesn't run under dev). Check `window.isSecureContext` if testing over a
  LAN IP or Tailscale hostname - no error, just silently no offline capability.
- **Moments**: rename, merge, auto-generate, create-from-selection, each validated by
  looking at the actual folder on disk afterward, not just the UI updating.
- **Mobile/touch**: pinch density, the phone layout's rail-as-collapsed-handle, viewport
  behavior. Real-device testing matters more here than anywhere else in this list - see
  "Emulation limits" below.

## Cold start, every time

`CLAUDE.md` already says this; it's worth restating because it's the single most common way
a real bug gets missed. A warm browser profile or a long-running dev session can hide a
first-load bug behind cached tokens, a warm module cache, or state left over from ten
minutes of manual clicking. Before calling something fixed:

```sh
# Fresh server process, fresh browser context, no reused state.
pkill -f "uvicorn api:root_app" 2>/dev/null   # careful: matches the shell's own command
                                                # line too under some shells - check what
                                                # died before assuming it was the server
LESPHOTOS_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))') \
  LESPHOTOS_DATA_DIR=<scratch dir> \
  uvicorn api:root_app --app-dir server --port 8001 &
```

Pair with a fresh Playwright `browserContext` (not a reused page) or an actual private
window if testing by hand.

## Emulation limits (be honest about what a check actually proved)

Chromium's touch/pinch emulation (via CDP `Input.synthesizePinchGesture`) does not
reproduce the browser's own native pinch-to-zoom arbitration - in practice this means an
emulated check can prove your gesture handler works, but **cannot** prove it successfully
prevents the browser's native zoom, because that native zoom never triggers under emulation
even on old, known-broken code. This came up validating the Android pinch fix: the fix was
confirmed mechanically correct (touch-action + non-passive touchmove is the standard
prevention pattern, and the gesture handler itself was proven to stop stalling after one
step), but the actual "does Chrome still zoom the page" question needs a real device. Say so
explicitly in any report that relies on touch emulation for a gesture-conflict class of bug.

## Writing an ad hoc Playwright script (gotchas hit in practice)

For a one-off manual check that isn't worth adding to the committed suite (see
"Ad hoc vs. committed scripts" below):

- **No `chromium-cli` is installed in most environments this runs in** — don't spend a
  turn checking for it. Playwright itself is already a project dependency
  (`client/web/node_modules/playwright`), with Chromium binaries already cached from
  prior runs at `~/Library/Caches/ms-playwright/` on a Mac. Use it directly.
- **Run the script from inside `client/web/`**, not a scratch/tmp directory —
  `node script.mjs` needs to resolve `playwright` from `client/web/node_modules`, and
  running from elsewhere throws `ERR_MODULE_NOT_FOUND` even though the package is
  genuinely installed. Put a throwaway script in a scratch subfolder there (e.g.
  `.scratch/`) and delete it when done — don't commit it.
- **Use `chromium.launchPersistentContext(profileDir, {...})`**, not a plain
  `chromium.launch()` + `newContext()`, when a check needs to survive across *separate*
  script invocations — e.g. "sync once to build a watermark, then run a second script
  to prove the second sync is cheap." A plain context's localStorage/IndexedDB don't
  persist between Node process runs; a persistent context backed by a real profile
  directory does.
- **`page.waitForFunction(fn, options)` is wrong** — the real signature is
  `waitForFunction(fn, arg, options)`. Passing an options object as the second
  positional argument silently makes it the page function's `arg` instead, so a
  `{timeout: 900000}` meant to extend the wait never takes effect and the call falls
  back to Playwright's 30s default — this can look exactly like the thing you're
  testing hanging or stalling, when it's actually your own script's timeout firing
  early. Always pass `null` (or the real arg) as the second positional parameter,
  options third.

## Ad hoc vs. committed scripts

Same rule as `CLAUDE.md` already states for scratch scripts generally: write throwaway
Playwright checks in the scratchpad (cloud sessions) or wherever's convenient (local), and
delete them after - **unless** the check is worth re-running on every future change, in
which case add it to the committed suite below rather than leaving it as a loose script.

## Committed end-to-end tests

`client/web/tests/` (`@playwright/test`) is the maintained regression suite - what the
earlier ad hoc Android-fix scripts turned into once they were worth keeping.

```sh
cd client/web
npx playwright test          # headless, all projects
npx playwright test --ui     # interactive runner, for writing/debugging a spec
```

**Fully self-contained.** `playwright.config.ts`'s `webServer` runs
`tests/setup/test-server.sh`, which bootstraps and serves an isolated instance: its own data
dir (`tests/.data/`, gitignored), a small fakelib library, one `e2e` account, and the real
production build (`api:root_app`, not `vite dev` - matching how `CLAUDE.md` says validation
should happen) on its own throwaway port (`:8199` by default). It never touches the
developer's own `:8001`/`:5173` dev instances or their real login session, and the bootstrap
is idempotent - safe to leave running and re-invoke `npx playwright test` repeatedly while
iterating on a spec.

Two projects run every spec: `desktop-chromium` and `mobile-android` (a real Pixel 7 device
profile with touch). A spec that only makes sense on one skips itself on the other via
`test.skip(({ isMobile }) => ...)` / `test.skip(({ browserName }) => ...)` - see
`mobile-layout.spec.ts` and `pinch-zoom.spec.ts` for the pattern.

**`workers: 1`, deliberately.** All projects share one backend and one library, so two
copies of a state-mutating test (e.g. `favorite-no-refresh.spec.ts`, running once per
project) racing on the same live data produces a very convincing but entirely test-caused
failure - this is exactly what happened once during this suite's own development, diagnosed
down to a shared-state race before the fix. A bigger suite would want per-test isolated
libraries instead of serializing; this one is small enough (~20s total) that trading
parallelism for determinism is the easy call.

**Click vs. tap, and why `favorite-no-refresh.spec.ts` doesn't assert exact scrollTop
equality.** Investigated at length during development: on `mobile-android` specifically,
`@playwright/test`'s own `.click()` (via the test runner) nudges scrollTop by a few hundred
pixels even against a button already fully inside the viewport - reproducible and
deterministic, but absent when the identical interaction runs via bare `playwright-core`
(no test runner) or via a real `page.touchscreen.tap()`. Read as an internal "make sure this
is robustly interactable" scroll correction belonging to the test runner's own action
implementation, not the app - there is no `scrollIntoView`/`.focus()` call anywhere near the
favorite-toggle code, confirmed by reading it. The spec accounts for this rather than fights
it: it asserts the actual regression signature (no "Loading…" flash, scroll doesn't reset
toward the top) instead of pixel-exact equality. If a future spec hits the same symptom,
this is the explanation to reach for before assuming a product bug.

**A tile is `[data-testid=photo-tile]` on either its `<img>` or its placeholder `<div>`** -
not `img[alt]`. Thumbnail *pixels* are opt-in (`allowNetwork: false` in `useThumbnail.ts`;
see `CLAUDE.md`'s "thumbnailBlob is optional" gotcha), so a freshly-synced grid renders
placeholders only, same as real first-use - a test that waits for `<img>` specifically will
time out against a library that hasn't run "Download Thumbnails" yet.

## Automated tests

`server/tests/` (pytest) covers pure logic and one security-critical path check - things
cheap enough to run in under two seconds with no server, no browser, no real files:

```sh
uv sync --group dev
uv run pytest server/tests -v
```

Covers: `moment_dates.py`'s date parsing (including the exact "9999-99-99 passes a naive
check" case that motivated writing a shared parser), `fakelib/grouping.py`'s gap logic
(including the deliberate no-split-at-New-Year behavior), `api._resolve_host_path` (the one
function standing between a client-supplied path and the filesystem - invariant #5 in
`CLAUDE.md`), and `auth.py`'s scrypt round-trip (regression coverage for the `maxmem`
gotcha - omitting it raises "memory limit exceeded" at import time, not gracefully).

**This does not replace real-app validation.** It exists because these specific functions
are (a) pure, (b) previously broke silently in exactly the way a two-line test would catch,
and (c) too fast to skip. It deliberately does not attempt to test anything that needs the
filesystem at scale, a real image decode, or the browser - those stay in the "drive the real
app" category `CLAUDE.md` describes, because that's what actually catches the bugs specific
to this codebase (EXIF edge cases, Safari's IndexedDB cost curve, the DS124's memory
ceiling). Add a new pure-function test when you fix a bug in one - not as a general habit of
converting every change into a test.

## Benchmarking

Speed and memory at real library size is a named concern for this app (the DS124 target has
1GB RAM and a spinning disk), and until now nothing tracked it over time - `CLAUDE.md`'s
own gotchas section is full of specific numbers found once by hand (e.g. "162MB → 110MB",
"193MB", "265ms / 13.5s") that were never captured anywhere re-runnable.

```sh
python3 tools/bench.py --root <library> --tag <machine-tag> [--derive-limit N]
python3 tools/bench_compare.py [--tag <machine-tag>]
```

Two non-destructive phases, both run as **fresh subprocesses** (so `peak_rss` - a
cumulative high-water mark - is isolated per phase rather than carrying over):

- `incremental_scan` - `indexer.py --no-derive` against an already-indexed library. This is
  the walk+skip-check cost, i.e. what runs on every startup/watcher tick regardless of
  whether anything changed.
- `derive_pending` - `indexer.py --derive-pending --limit N`. Actual decode+thumbnail cost.
  Does nothing on a fully-derived library by default; point `--root` at a library with some
  pending work, or accept that this phase reports zero.

Results append to `benchmarks/results.jsonl` (git-tracked - the log's own commit history is
the record). **`--tag` identifies the machine and library, and only same-tag runs are ever
comparable** - a laptop and the DS124 NAS are different hardware entirely, and a fake
1,000-photo library and the real 26,000-photo one are different problems. Suggested tags:
`bob-laptop-26k` for the primary real-library benchmark, `nas-nas` for the actual
production target, `container-fake-<n>` for a cloud session's fakelib run (useful for
catching a *directional* regression - "this commit made scanning 3x slower" is true
regardless of absolute hardware - but never cite a container number as representative of
NAS performance).

When to run it: before/after any change plausibly affecting indexer throughput or memory
(anything touching image decode, EXIF/metadata parsing, the derive pipeline, or the DB write
path), on whichever tier you have access to. On Tier 2 (the 26k laptop library), this is the
number that actually matters for the app's real constraint; report it explicitly when you
have it, and don't let a Tier-1 fake-library number stand in for it without saying so.

`tools/bench_compare.py` flags >10% regressions (`--threshold` to change) but doesn't fail a
build - a flagged regression needs a human/agent judgement call (real regression vs. one
noisy run vs. a deliberate tradeoff), not an automatic gate.

## Versioning

See `docs/decisions/versioning.md` for the full scheme. Short version for testing purposes:
the server's `/health` endpoint and the client's profile menu both show the running
`VERSION` - after a deploy or a local restart, checking that number is a fast way to confirm
you're actually testing the build you think you are, especially relevant after the NAS
restart steps in `CLAUDE.md` (which previously only confirmed *a* process came back up, not
*which* one).
