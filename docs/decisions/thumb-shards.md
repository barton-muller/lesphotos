# Decision: pre-assembled zip "shards" for bulk thumbnail download

## The problem

"Download all for offline" (see the client's `downloadAllThumbnails`) fetches every
thumbnail the device doesn't have yet. Even after batching many thumbnails into one zip
response per HTTP request (`/media/thumbs-batch` - see that endpoint's own docstring for
why batching mattered at all: IndexedDB serializes readwrite transactions per store, and
HTTP/1.1 caps the browser at 6 connections per origin), each request still means the
server statting and reading up to `MAX_THUMB_BATCH` individually-scattered files
(`cache/<photo_id>/thumb.webp`) before it can respond.

That scattering is free on an SSD. It is not free on the DS124's actual disk: a real
Toshiba HAT3300 spinning HDD (confirmed via `rotational=1` on the live NAS, not assumed),
where each small random read costs a real seek (~8-15ms typical for this drive class). A
300-photo batch is up to 300 seeks - a few seconds of pure seek latency the CPU-bound zip
framing itself never showed up as (measured: zip assembly overhead over a plain read is
single-digit milliseconds for a few hundred files, completely dominated by disk I/O on a
cold cache).

## Rejected approaches

**Hash-bucket shards** (`bucket = hash(photo_id) % N`). Considered first because it's
maximally stable against reorganizing moments (doesn't look at paths at all). Rejected
once the real usage pattern was worked through: a small incremental upload (e.g. 100 new
photos from a phone) lands scattered across up to 100 different hash buckets, each of
which would then need a full rebuild even though 99% of its content didn't change -
exactly backwards from "small uploads should be cheap."

**Moment-keyed shards** (one zip per moment folder). Rejected outright - the user
reorganizes moments often (rename, merge, split), and tying a cache to moment identity
would mean near-constant invalidation for the one thing this design most needs to be
stable against.

**Rebuild the open shard's zip on every new photo.** `zipfile`'s append mode makes an
incremental add cheap when the underlying files are warm (~16ms to append 10 entries to
an existing 290-entry zip, measured) - but a rebuild-from-scratch would mean re-reading
every already-known file in the shard from cold disk each time, which at the same ~10ms/
seek estimate is several seconds PER new photo during a bulk upload session. Append mode
sidesteps this entirely: only the new entry's bytes are read and written, never the
existing ones.

## Decision: shards keyed by DERIVATION order, frozen permanently once full

- A shard is a zip of up to `SHARD_SIZE` (500 - not NAS-measured, see below) thumbnails,
  built incrementally as photos are derived (`thumb_shards.assign_and_append`, called from
  `indexer.derive_pending` right after a successful derive).
- Assignment is by the order photos were **derived**, not their capture date
  (`taken_at`). `derive_pending` processes its backlog newest-first (see
  staged-thumbnail-generation.md), so keying by capture date would mean a single
  derive-pending run - even one just catching up 100 new phone photos - could touch
  shards spanning the whole library's date range instead of just appending to the
  current tail.
- A photo is assigned to a shard exactly once and never moves - not on a later
  re-derive (see "Known trade: re-derive doesn't refresh a frozen shard" below), not
  when its moment folder is renamed or merged (shard membership only ever reads
  `content_hash`/`photo_id`, never `path` - this is what makes it immune to
  reorganizing).
- A shard **freezes permanently** - its zip is never rebuilt - the moment it reaches
  `SHARD_SIZE` members. Exactly one shard is ever open (still being appended to) at a
  time.
- Frozen shards are served whole by `/media/thumb-shards/{shard}`; the still-open shard
  has no endpoint of its own and falls back to the existing `/media/thumbs-batch`
  per-id path, since its zip isn't guaranteed complete yet.

## Why this survives the two scenarios that matter

**Reorganizing moments** (the stated, frequent case): shard membership never reads a
path, so renaming/merging/moving a moment does nothing to any shard, frozen or open.

**A small incremental upload after "everything's already downloaded"** (the other
stated, frequent case - phone → Synology Photos → auto-derive → open the app): the new
photos extend the (already-open, already small) tail shard via cheap append-mode
writes. Every previously-frozen shard is completely untouched - no rebuild, no
re-read, regardless of how many shards already exist.

## Known trade: re-derive doesn't refresh a frozen shard

If a photo's thumbnail is regenerated after its shard has frozen (a manual re-derive,
not something that happens in normal operation), the frozen shard's zip keeps serving
the OLD thumbnail bytes for that one photo - `assign_and_append` sees an existing
`thumb_shard_members` row and returns immediately without touching the zip.

Accepted rather than solved, because:
- Derivatives are already treated as immutable per photo_id elsewhere in this codebase
  ("a re-derive writes the same content for the same id" - see api.py's derivative
  endpoints' own long-cache-header reasoning). A genuine re-derive producing
  *different* bytes for the same photo (e.g. a fix to `derive_photo` itself changing
  pixel output) is already an edge case outside that assumption, not a normal
  occurrence this system needs to defend against by default.
- If that assumption is ever broken deliberately (a real thumbnail-generation bug
  fix that needs to reach already-shard-assigned photos), the fix is a manual
  one: unfreeze the affected shard(s) in the DB and delete their zip, then
  re-run `--backfill-shards` to rebuild them from the now-correct on-disk
  thumbnails.

## Ordering: newest content must still be visible first

The client's "download all" is explicitly newest-first (matching `derive_pending`'s own
"the photos someone actually sees get pixels first"). Shard 0 is the OLDEST derived
content, so a naive "frozen shards in shard-number order, then the open shard" fetch
sequence would show old photos before new ones - inverted from the whole point.

The client instead: (1) fetches the still-open shard's content via the batch endpoint
first (it's the newest, by construction - shards fill oldest-to-newest), sized from the
manifest's `member_count` for the open shard; (2) fetches frozen shards
newest-shard-number-first; (3) a final batch-endpoint catch-all for anything neither
step covered (a shard's zip missing an entry, or the size estimate in step 1 being off
because a shard froze between the manifest fetch and now).

## Numbers not yet validated on the actual NAS

`SHARD_SIZE = 500` and the client's `CONCURRENCY`/`HTTP_BATCH_SIZE` constants are sized
from Mac-only measurements (fast SSD, 8 cores, 16GB RAM) extrapolated with arithmetic
(e.g. "500 photos × ~20KB ≈ 10MB transient buffer, trivial against 968MB"), not measured
on the DS124 directly. The DS124 has 968MB total RAM and is already under real memory
pressure at idle (~167MB free measured via SSH). Revisit these constants with real
on-device numbers before assuming they're right for production, not just for a Mac dev
server.

## Migrating an existing library

`derive_pending` only ever selects `derived = 0` rows, so a photo derived before this
system existed would never get a shard assignment on its own. `indexer.py
--backfill-shards` is the one-time catch-up: walks every already-derived photo with no
`thumb_shard_members` row, oldest-`taken_at`-first (the real derivation order is lost
for these, since they predate this system entirely - capture-date order is an accepted
approximation ONLY for this one-time backfill, never for the ongoing per-derive
assignment in `assign_and_append`, where the real derivation order is available and
used). Safe to interrupt and re-run - each photo's assignment commits in batches, and an
already-assigned photo is excluded by the query itself.

Verified against a real 26,498-photo library: 100% shard coverage after backfill (53
shards, 52 frozen + 1 open), zip integrity checked across several shards, full "download
all" completed in under 3 minutes (down from an estimated ~3.5 hours before any of this
batching/sharding work).
