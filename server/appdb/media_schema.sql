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
    is_motion_clip INTEGER NOT NULL DEFAULT 0,
    -- The photo's MOMENT date (its containing folder's own date prefix,
    -- see moment_dates.py), NOT taken_at. /timeline sorts by this first so
    -- a moment's photos stay together even when one photo's own capture
    -- time disagrees with the folder (see /timeline's docstring in api.py).
    -- NULL for a folder with no parseable date prefix - sorts last.
    -- Derived from the path, like everything else here: rebuildable by
    -- re-indexing, never a second source of truth for the folder name.
    moment_date TEXT,
    -- Monotonic change counter, bumped by db.bump_changed_seq() on every
    -- write that should be visible to a delta /timeline?since= fetch (see
    -- docs/decisions/delta-sync.md). NOT an alias for sqlite's own rowid:
    -- content_hash is this table's PRIMARY KEY and it's TEXT, so rowid
    -- exists as a hidden column but was never something client code could
    -- name or rely on - this column is the real, explicit, indexed cursor.
    -- 0 for a row that predates this column (pre-migration), which a
    -- since=0 delta fetch naturally treats as "already known", not new.
    changed_seq INTEGER NOT NULL DEFAULT 0
);

-- The timeline query is ORDER BY taken_at DESC with a LIMIT/OFFSET, so this
-- index is what keeps it sub-millisecond instead of a full sort of every
-- row in the library.
CREATE INDEX IF NOT EXISTS idx_media_taken_at ON media(taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_derived ON media(derived);

-- idx_media_changed_seq is created by db.connect() AFTER its column
-- migration, not here - same reason as idx_media_motion below: on a
-- pre-existing database this script runs before changed_seq exists, and
-- indexing a missing column is a hard error.

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

-- Thumbnail zip shards (see docs/decisions/thumb-shards.md), a cache of
-- pre-assembled zip archives so a bulk "download all" doesn't cost one
-- scattered-file disk seek per photo on the NAS's spinning HDD.
--
-- Assigned by DERIVATION order (see derive_shard/derive_seq below), not
-- taken_at: derive_pending processes its backlog newest-first, so if
-- shards were keyed by capture date, one derive-pending run could touch
-- (and invalidate) shards spanning the whole library instead of just
-- appending to the current tail. Derivation order is monotonic and
-- append-only regardless of what dates the photos themselves carry, and
-- is completely unaffected by moment renames/merges (those only ever
-- touch a photo's `path`, never this).
--
-- A shard is either OPEN (frozen = 0: still being appended to, must be
-- rebuilt/re-fetched live rather than trusted as complete) or FROZEN
-- (frozen = 1: has exactly SHARD_SIZE members - see indexer.py - and its
-- zip is permanent and never touched again). Exactly one shard is ever
-- open at a time; a new one starts only when the current one reaches
-- SHARD_SIZE.
CREATE TABLE IF NOT EXISTS thumb_shards (
    shard INTEGER PRIMARY KEY,
    frozen INTEGER NOT NULL DEFAULT 0,
    member_count INTEGER NOT NULL DEFAULT 0
);

-- Which shard each derived photo belongs to. One row per photo that has
-- ever been derived; a photo is assigned exactly once, at derive time,
-- and never moves shards afterward (even across a re-derive - see
-- thumb-shards.md's "re-derive" scenario for why a stale entry in a
-- frozen shard's zip is an accepted, documented trade rather than
-- something this table tries to detect).
CREATE TABLE IF NOT EXISTS thumb_shard_members (
    content_hash TEXT PRIMARY KEY,
    shard INTEGER NOT NULL,
    photo_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thumb_shard_members_shard ON thumb_shard_members(shard);
