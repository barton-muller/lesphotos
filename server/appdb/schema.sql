-- App-owned state, separate from PhotoView's own (disposable) database.
-- content_hash is the stable identity: PhotoView's own media/album IDs are
-- confirmed unstable across any filesystem rename/move (see plan.md), so
-- this is the source of truth for anything a rename must not lose.

-- photo_id is the client-facing stable identity (see
-- docs/decisions/stable-photo-id.md): a UUID minted once per path and kept
-- forever, independent of both PhotoView's id (churns on every move) and
-- content_hash (churns on in-place edits). content_hash stays for what it's
-- actually good at - duplicate-file detection - not identity.
-- photo_id has no column-level UNIQUE/index here: a pre-existing database
-- created before this column existed can't have it added via CREATE TABLE
-- IF NOT EXISTS (which no-ops if the table already exists) - db.connect()
-- runs an ALTER TABLE + index migration for that case instead. This
-- CREATE TABLE only fires for a genuinely fresh database.
CREATE TABLE IF NOT EXISTS photos (
    content_hash TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    favorite INTEGER NOT NULL DEFAULT 0,
    photo_id TEXT
);

CREATE TABLE IF NOT EXISTS moments (
    moment_id TEXT PRIMARY KEY,  -- stable id we mint, e.g. a uuid
    path TEXT NOT NULL UNIQUE,   -- current folder path
    label TEXT NOT NULL          -- current folder name, e.g. "2024-03-10-LeedsWithDad"
);
