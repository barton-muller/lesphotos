-- Media metadata owned by the first-party indexer (see
-- docs/decisions/replacing-photoview.md). Split into its own file rather
-- than extending schema.sql's CREATE TABLE, for the same reason photo_id
-- needed an ALTER TABLE migration: CREATE TABLE IF NOT EXISTS no-ops on an
-- existing table, so new columns on `photos` have to be added by migration.
-- db.connect() applies both.

-- Derivative + scan state, keyed by the same content_hash as `photos`.
-- Separate table (not more columns on `photos`) because the lifecycle
-- differs: `photos` rows are user state that must survive anything, these
-- are regenerable cache bookkeeping that can be dropped and rebuilt.
--
-- mtime_ns/size are the incremental-scan key: the indexer stats a file and
-- skips it entirely when both match, touching zero image bytes. That is the
-- steady-state cost of "has anything changed?" and the reason a re-run of an
-- unchanged library is seconds rather than hours.
CREATE TABLE IF NOT EXISTS media (
    content_hash TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    -- Capture time, ISO8601. EXIF DateTimeOriginal where present, else the
    -- file mtime - never null, so the timeline's ORDER BY never has to
    -- handle a missing sort key.
    taken_at TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    is_video INTEGER NOT NULL DEFAULT 0,
    -- Seconds, videos only.
    duration REAL,
    -- Set once derivatives exist on disk; lets a killed indexer distinguish
    -- "registered but not yet thumbnailed" from "done" on restart.
    derived INTEGER NOT NULL DEFAULT 0,
    -- See the motion notes at the end of this file.
    motion_kind TEXT,
    motion_path TEXT,
    motion_offset INTEGER,
    -- 1 when this row IS the .MOV half of an Apple pair, so the timeline
    -- can hide it without losing the file.
    is_motion_clip INTEGER NOT NULL DEFAULT 0
);

-- The timeline query is ORDER BY taken_at DESC with a LIMIT/OFFSET, so this
-- index is what keeps it sub-millisecond instead of a full sort of every
-- row in the library.
CREATE INDEX IF NOT EXISTS idx_media_taken_at ON media(taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_derived ON media(derived);

-- Live Photo / Motion Photo linkage (see docs/decisions/live-and-motion-photos.md).
--
-- motion_kind:   'apple'  - the clip is a separate sibling file
--                'google' - the clip is embedded in this file
-- motion_path:   apple only; absolute path to the .MOV. Nullable, and the
--                filesystem stays the source of truth - this is a cache of
--                a pairing that can be recomputed by re-indexing.
-- motion_offset: google only; byte offset of the embedded MP4.
--
-- Held on `media` rather than `photos` because it is derived from file
-- contents, like every other column here, and a re-index must be free to
-- recompute it. `photos` is user state that must survive anything.
--
-- These columns are added by db.connect()'s migration for existing
-- databases; this CREATE TABLE only fires on a genuinely fresh one.

-- The idx_media_motion index is created by db.connect() AFTER its column
-- migration, not here: on a pre-existing database this script runs before
-- is_motion_clip exists, and indexing a missing column is a hard error.
