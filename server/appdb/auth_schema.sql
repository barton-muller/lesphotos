-- People, not photos. This is the only durable state that does NOT live
-- inside a photos folder (see config.py rule 2): it's about who may log
-- in, and it spans libraries.
--
-- No role/is_admin column, deliberately. There is no admin: every
-- mutation the app has (rename, merge, star, index) acts on exactly one
-- library, so "acting on your own library" covers all of them. An admin
-- would exist only to reach across users' photos, which is the one thing
-- this design is meant to make structurally impossible.
CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,   -- uuid, stable across renames
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash      TEXT NOT NULL,      -- scrypt, see auth.py hash_password
    -- Absolute path to this user's library. Not derived from the name:
    -- "could be somewhere else" - one user may sit on another volume.
    photos_root  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
