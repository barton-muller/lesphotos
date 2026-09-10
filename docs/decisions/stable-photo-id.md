# Stable per-photo id, decoupled from PhotoView's id and from content hash

> **Still current, and now load-bearing.** `photo_id` survives the backend replacement
> unchanged. Derivatives are keyed by it (`cache/<photo_id>/`), which is why a move doesn't
> invalidate a thumbnail and a rename costs ~60ms. The PhotoView comparisons below are
> historical.

## Problem

The client's IndexedDB cache (`client/web/src/db.ts`) is keyed by PhotoView's own media `id`.
PhotoView derives that id from `MD5(filesystem path)` (see CLAUDE.md invariant #1) - any
move/rename the app performs is seen by PhotoView as delete-old-row + create-new-row, so the
moved photo gets a brand new id. The client's sync (`sync.ts`) diffs ids against what's cached
and prunes anything no longer present remotely, which is *supposed* to clean up the orphaned
old-id row - but in practice this left stale duplicate-looking rows on screen (reported
directly by the user, reproduced: real browser IndexedDB retained old-id rows for photos moved
before a fix that made sync fire automatically after create/rename; a fresh sync alone did not
clear them either, meaning the diff/prune step itself isn't reliably converging).

Even setting the reliability question aside, keying identity off something that churns on
every move is the wrong design - the server side already solved this exact problem for
favorites/moment-labels by introducing `server/appdb`, keyed by content hash instead of
PhotoView's id. The client needs the equivalent fix.

## Why not content hash for the client's id too

`appdb.photos` already has `content_hash TEXT PRIMARY KEY`. Reusing it directly for the
client's id was the obvious first idea, but breaks down on inspection:

- **In-place edits change the hash.** If a photo is ever edited (crop/rotate/filter) via a
  phone app or any tool that rewrites the original file's bytes, the same logical photo gets a
  new hash on the next scan - the client would see it as a brand-new photo, silently losing its
  favorite state and moment history. The project's own invariant #5 says *this app* never
  mutates originals, but a phone's own camera-roll editor writing back into a Tailscale-synced
  folder is outside the app's control and a realistic real-world case for a family photo
  library.
- **Hashing is expensive at the real target scale.** appdb only computes a hash when a photo
  is first touched by a write path (favorite/rename/merge/create) - it has never had to hash
  every photo in the library. Backfilling a hash for all ~28,000 photos/user (the real design
  target, see CLAUDE.md) means reading every file's full bytes at least once; fine as a one-time
  migration, but re-hashing on every sync to detect edits would not fit the DS124's constraints.

## Design: server-minted UUID, id -> current-path

Add a `photo_id` column to `appdb.photos`: a UUID minted once, the first time a path is ever
seen by any server-side scan, kept forever after regardless of content changes or moves.

- **Survives moves/renames/merges**: invariant #3 already guarantees the app is the *only*
  thing that ever moves a file. Every reorganize function in `reorganize.py` already computes
  old_path -> new_path directly (it performed the move itself) - updating `photo_id`'s
  associated path on that same write is exact, not a heuristic (no inode-matching or
  path-guessing needed, unlike a generic filesystem-watcher approach).
- **Survives in-place edits**: identity doesn't depend on file bytes at all, so an edited photo
  keeps its id, favorite, and moment association.
- **Cheap**: minting a UUID is O(1) per photo, no file read required. A full-library backfill
  (new endpoint/scan pass, one-time) is just "any path in appdb without a `photo_id` gets one
  assigned," not a hash of every file.
- **content_hash stays** as a separate column for what it's actually good at: detecting two
  identical files (e.g. accidental duplicate imports) - not used as an identity key going
  forward.

## What changes

- `appdb/schema.sql`: add `photo_id TEXT UNIQUE` to `photos`, backed by an index.
- `appdb/db.py`: `upsert_photo` mints a UUID (`uuid.uuid4()`) when inserting a row that doesn't
  already have one; a new `ensure_all_photos_registered()` walks the full photo tree and
  registers any path not yet in appdb (covers photos never touched by a write path, needed
  since today appdb is populated lazily).
- `server/api.py`: new `GET /photo-ids` endpoint returning the full `path -> photo_id` map
  (calls `ensure_all_photos_registered()` first) - the client's sync fetches this once per sync
  pass and uses it to resolve each PhotoView timeline entry's stable id by path.
- `client/web/src/db.ts`: `CachedPhoto.id` becomes the server-minted UUID instead of
  PhotoView's id; schema version bump with an `.upgrade()` that clears the table (the old
  PhotoView-id-keyed rows have no valid mapping to the new id scheme, and a full resync from
  empty is cheap - this is a thumbnail cache, not a source of truth, so data loss here is not a
  concern per the project's own design principles).
- `client/web/src/sync.ts`: fetches `/photo-ids` alongside the timeline, resolves each media
  item's stable id by its `path`, and diffs/upserts/prunes using that id instead of
  `media.id`.
