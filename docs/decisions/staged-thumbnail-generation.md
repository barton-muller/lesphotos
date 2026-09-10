# Staged thumbnail generation

## The problem

A first index of a real library decodes every photo. On the DS124 that is hours, it
is all-or-nothing, and until it finishes the library is unusable. Adding a user meant
committing the NAS to a long uninterruptible job.

## What was decided

Split **registration** from **derivation**.

```sh
# Phase 1 - metadata only. No decoding.
indexer.py --user alice --no-derive

# Phase 2 - thumbnails, a batch at a time, resumable.
indexer.py --user alice --derive-pending --limit 200
indexer.py --user alice --status
```

## Why

The expensive part of indexing was always the decode, never the metadata. `Image.open()`
reads the header without decoding pixels, so registration costs one small read even for
a 24MP HEIC — the metadata pass runs at filesystem speed (measured: 12 files in 0.01s,
30MB peak RSS).

The `media.derived` column already existed, to let a killed indexer distinguish
"registered but not thumbnailed" from "done". Staging is that flag put to deliberate
use rather than new bookkeeping: `--derive-pending` selects `derived = 0`, newest first,
and commits per batch. Interrupt it at any point and re-running resumes exactly where it
stopped.

Newest-first because the timeline opens at the top: the photos someone actually sees get
pixels first.

## Consequence: the library fills in progressively

`/timeline` filters on `derived = 1` (pre-existing), so a photo appears only once it has
a thumbnail. This was kept rather than changed — a tile with no thumbnail is a broken
image, which is worse than a photo that has not arrived yet. So during a backlog the
timeline grows as batches complete, which is the intended staged experience.

Verified end to end: 12 registered with `--no-derive` → timeline empty → `--limit 5`
→ 5 visible → remainder → all visible; re-running with nothing pending is a no-op.

## Doing the bulk on a faster machine

Derivatives are keyed by `photo_id`, which is stable forever (see
`stable-photo-id.md`), so a cache built on the Mac is valid on the NAS unchanged.
`indexer.py --root <path>` works against a mounted share with no account and no auth
database; the result is then an ordinary folder copy, because a library's database and
thumbnails both live in its own `.lesphotos/` sidecar (see `per-user-libraries.md`).

Copy `.lesphotos/` as a unit, with the server stopped: the database records which photos
have derivatives, so moving one without the other leaves them disagreeing, and SQLite
does not tolerate its file changing underneath a running reader.
