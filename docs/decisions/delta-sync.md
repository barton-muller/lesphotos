# Decision: a changed_seq watermark for delta timeline sync, and trash as a hidden moment

## The problem

Every metadata sync (`syncTimeline` in `sync.ts`) that didn't hit the old cheap
count-only short-circuit did a **full relist**: paginate the entire `/timeline`,
diffing every row against IndexedDB client-side. Measured at ~10s over Tailscale at
4,000 photos, and confirmed directly on the real NAS against a real 26,451-photo
library: **54 requests, 51 seconds** for a full relist. That's the cost paid for adding
a single new photo — the single most common sync trigger — same as syncing after weeks
away. The existing `/timeline/count` short-circuit only caught additions/deletions,
missing a same-count change entirely (a favorite toggle, a rename).

## Rejected approach: a text log + client-side diff ("git diff" model)

The user's first instinct was a plain-text log of all photos on both sides, diffed like
`git diff`. Rejected: a diff needs a full copy of the current state to diff *against*,
so this doesn't avoid transferring the whole library on every sync — it just moves the
diffing work client-side instead of letting an indexed SQL query do it for free. It
scales with **library size**, not with what actually changed, which is the wrong axis
for "I added one photo."

## Rejected approach: a full event-log / change-feed table

Considered (append an `INSERT`/`UPDATE`/`DELETE` row to a dedicated table on every
write, client tails `GET /changes?since=N`). Rejected as overkill for this project:
this app has no multi-writer contention to reason about (invariant #6/#7 — one
library per session, no admin, filesystem is sole source of truth), so there's no need
for a durable audit trail, only a cheap "what's newer than what I've seen" signal. A
plain monotonic counter column gives that directly, with no new table.

## Decision: `media.changed_seq`, a single global counter

- New column `media.changed_seq INTEGER NOT NULL DEFAULT 0`, migrated via `db.py`'s
  existing `ALTER TABLE` pattern (same as `is_motion_clip`/`moment_date` before it) —
  **the new column's index must be created in `db.py`'s migration block, not the
  static `media_schema.sql`**, since the static schema's `CREATE INDEX` runs
  unconditionally via `executescript` before the migration adds the column on an
  existing database — caught by actually running the migration against the real
  2,300-photo test library and 26,500-photo NAS library before shipping, not by
  reading the code.
- `db.bump_changed_seq(conn, content_hash)`: `UPDATE media SET changed_seq =
  (SELECT COALESCE(MAX(changed_seq), 0) + 1 FROM media) WHERE content_hash = ?`. One
  shared helper, called from every write path that should be visible to another
  device's delta sync: the indexer's main upsert and its `derived=1` post-derive
  update, motion-pairing updates, `rebalance_month_bucket_dates`,
  `backfill_moment_dates`, and — critically — `db.upsert_photo` (which covers
  favorites and every path-registration on a move), since without that a favorite
  toggle would silently never propagate through delta sync at all.
- `MAX(changed_seq)+1` rather than a separate counter table: this table is already
  what every delta reader scans, so the two can't drift, and at this project's write
  volume (one interactive user, plus an indexer that already serializes its own
  writes) the extra read costs nothing worth a second moving part.
- `/timeline/max-seq`: `SELECT COALESCE(MAX(changed_seq), 0)`. Replaces
  `/timeline/count` as the client's short-circuit check — catches every write that
  bumps the counter, not just additions/deletions.
- `/timeline?since=N`: returns `{items: [...], maxSeq: N}` — a change feed ordered by
  `changed_seq ASC`, not the display's `moment_date DESC` ordering (a delta page may
  span the whole library's history, so display order has no meaning here). **`maxSeq`
  in the response must be the last row's own `changed_seq`, not the library's global
  max** — a real bug caught before shipping: if `limit` truncates the page and the
  client advanced its watermark to the global max anyway, every row between the
  truncated page's end and the global max would be silently skipped forever (they'd
  all satisfy `changed_seq <= since` on the client's next call). An empty page falls
  back to returning `since` unchanged, since nothing advanced.
- Client (`sync.ts`): `readLastSeq()`/`writeLastSeq()` persist the watermark in
  `localStorage`, same mechanism the old count-only check used. `syncTimeline` tries
  the cheap `max-seq` check first; if it matches, returns immediately; if not, fetches
  only the delta via `fetchTimelineDeltaPaged`. No cached watermark (first sync on a
  device, or a server predating delta sync entirely — no `maxSeq` in the response) falls
  back to the full relist, which also establishes the watermark for every sync after.

## Trash: reusing the same watermark, not a special-cased tombstone

Building delete required deciding how a client's delta sync learns "this photo is
gone." Two designs were considered:

**Rejected: DB tombstone (`deleted=1` flag)**. Would need `/timeline`'s delta mode to
include tombstoned rows and the client to special-case a `deleted` field, plus a
separate meaning for "gone" vs. every other kind of change.

**Rejected: `.lesphotos/trash/` (the sidecar dir).** The first design considered —
until re-reading CLAUDE.md's own invariant #1 ("the filesystem is the only permanent
source of truth... rebuildable by re-indexing"). `.lesphotos/` is exactly the directory
the indexer's own walk skips (`iter_media_files`'s leading-dot rule — it has to, or the
indexer would try to index its own thumbnail cache). A trashed photo living there would
be invisible to any future full re-index, silently and permanently lost on a rebuilt
appdb — the opposite of what invariant #1 promises. Caught before writing any code, by
checking `iter_media_files` directly rather than assuming.

**Decision: trash is a hidden moment.** `POST /photos/delete` moves selected photos to
`library/trash/YYYY/YYYY-MM-DD/` via the *existing* `reorganize.move_photos` primitive
— a plain, real, indexer-walked directory, not a sidecar path. This means:
- A trashed photo stays a fully ordinary `media`/`photos` row: same `photo_id`, same
  `changed_seq` bump (via the same `_reindex_moved` call site every other move already
  uses), same delta-sync visibility as any other move. No new server-side concept.
- `/timeline`'s **default** (no `since`) query gets one added filter — `path NOT LIKE
  '<trash-prefix>%'` — so the everyday grid doesn't show it. The **delta** (`since=`)
  query does *not* filter it out; a trashed photo arrives through the delta feed like
  any other moved row, carrying its new trash path.
- The client (`sync.ts`'s `isTrashed()`) checks the path prefix and `bulkDelete`s that
  row locally instead of writing it — a client-side display decision, not a
  sync-correctness one, mirroring the server's own default-query filter.
- A future "view trash" UI is therefore just "stop applying this one filter," not a new
  endpoint or a new sync path.
- Full rebuild survivability confirmed directly: wiped a test library's `appdb.sqlite3`
  and re-ran the indexer from scratch — the trashed file was re-discovered and
  re-registered normally, since it's an ordinary walked path.
- `move_photos`'s `dest_moment_path.mkdir(exist_ok=True)` needed `parents=True` added —
  every prior caller's destination sat directly under an already-existing year folder,
  so the no-parents behavior was never exercised until `trash/YYYY/YYYY-MM-DD/` needed
  two new levels created at once on a library's first-ever delete. Caught live via curl
  against the NAS, not by reading the code.

## Verified

- Server-side: `curl` against `/timeline?since=`, `/timeline/max-seq`, and
  `/photos/delete` directly on both a local 2,300-photo test library and the real NAS.
- Client + server together, in a real browser (Playwright, headless Chromium — see
  `browser-testing-approach` memory for the harness gotchas hit setting this up):
  - **Local Mac, synthetic library**: baseline full relist 6 requests/1.4s; after
    adding one photo, delta sync **2 requests/631ms**.
  - **Real NAS, real 26,451-photo library**: full relist **54 requests/51.1s**; a
    no-op resync (nothing changed) **1 request**, effectively instant; after adding
    one photo, delta sync **2 requests**, confirmed the new photo's row landed
    correctly in IndexedDB via direct inspection.
  - **Trash on the real NAS** (disposable `test` library): file physically moved
    server-side, delta feed carried the new trash path, client correctly dropped the
    tile without a full relist.
- 409 handling on `/moments/rename`/`/moments/merge` (a stale client-supplied path,
  e.g. another device already renamed/merged/deleted it) already existed server-side
  (`_run_reorganize`) but the client only showed a generic error string — added a
  `/\(409\)/` check (matching the existing `/\(401\)/` pattern) in `Timeline.tsx` that
  shows a clearer message and triggers an automatic resync instead.

## Known follow-up, not part of this change

Favoriting a recently-uploaded photo turned out to separately 500 on the real NAS for
an unrelated reason — the server process couldn't read a different DSM user's `700`
phone-upload files. Real bug, but a filesystem-permissions problem, not anything to do
with delta sync (confirmed: the delta-sync mechanism itself worked correctly against
an older, normally-permissioned test photo before the permissions bug was found). See
`docs/known-issues.md` for that investigation and fix.
