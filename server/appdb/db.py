"""
The app's own state, kept separate from PhotoView's database. See plan.md's
"Critical finding: renaming a file breaks its identity" section for why this
exists: PhotoView's media/album IDs are not stable across a filesystem
rename or move, so favorites and moment labels cannot live only in
PhotoView's DB if the app is going to let users rename/merge moments.

This database is itself disposable in the sense that matters for the
project's design principles - path/label state can always be rebuilt by
rescanning the filesystem - but favorite flags are user data with no other
source, so this file is the one piece of state in the whole system that
actually needs backing up.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
MEDIA_SCHEMA_PATH = Path(__file__).resolve().parent / "media_schema.sql"

# Stars are mirrored to a plain-text file that survives even if this app
# and its database disappear - readable with a text editor in 20 years.
# appdb (SQLite) stays the fast, real-time source of truth for the running
# app; that file is the durability copy, refreshed after writes rather
# than being written to directly. It now lives inside the library's own
# .lesphotos/ sidecar (config.Library.stars_json_path), so it moves with
# the photos it describes.


class _Conn(sqlite3.Connection):
    """sqlite3.Connection has no __dict__, so an attribute can't just be
    stuck on it - a subclass is the supported way to carry the library
    along with the connection."""
    library = None


def connect(library):
    """
    Opens one library's database.

    Takes the library explicitly rather than reading a module-level
    DB_PATH, which is what made this single-tenant. The Library is stashed
    on the connection so helpers that need to write the stars mirror can
    find the right path without every caller threading it through.
    """
    library.ensure_dirs()
    conn = sqlite3.connect(library.db_path, factory=_Conn)
    conn.library = library
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    # CREATE TABLE IF NOT EXISTS above doesn't add columns to an
    # already-existing table (a pre-photo_id database from before this
    # migration existed) - ALTER TABLE ADD COLUMN, guarded by a pragma
    # check so it's safe to run on every connect.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(photos)")}
    if "photo_id" not in existing_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN photo_id TEXT")
        conn.executemany(
            "UPDATE photos SET photo_id = ? WHERE content_hash = ?",
            [(str(uuid.uuid4()), row["content_hash"]) for row in conn.execute("SELECT content_hash FROM photos")],
        )
        conn.commit()
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_photos_photo_id ON photos(photo_id)")
    conn.executescript(MEDIA_SCHEMA_PATH.read_text())
    # Same reason as photo_id above: CREATE TABLE IF NOT EXISTS no-ops on an
    # existing `media` table, so the motion columns have to be added by
    # migration for any database created before they existed - which
    # includes the live one on the NAS.
    media_cols = {row["name"] for row in conn.execute("PRAGMA table_info(media)")}
    for col, decl in (
        ("motion_kind", "TEXT"),
        ("motion_path", "TEXT"),
        ("motion_offset", "INTEGER"),
        ("is_motion_clip", "INTEGER NOT NULL DEFAULT 0"),
        ("moment_date", "TEXT"),
    ):
        if col not in media_cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {col} {decl}")
    if media_cols and "is_motion_clip" not in media_cols:
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_motion ON media(is_motion_clip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_moment_date ON media(moment_date DESC)")
    # WAL lets the indexer write while the API serves reads, instead of the
    # two blocking each other - the indexer is a long-running writer, which
    # is exactly the case rollback-journal mode handles worst. NORMAL sync
    # is the standard WAL pairing: a crash can lose the last commit, which
    # for regenerable derivative bookkeeping is an acceptable trade for not
    # fsyncing on every batch on a NAS's disk.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def content_hash_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_photo(conn, content_hash, path, favorite=None):
    existing = conn.execute(
        "SELECT favorite, path, photo_id FROM photos WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if favorite is None:
        favorite = existing["favorite"] if existing else 0
    # photo_id is minted once and never overwritten (see
    # docs/decisions/stable-photo-id.md) - ON CONFLICT deliberately excludes
    # it from the UPDATE SET clause so a re-upsert of an already-known
    # content_hash (e.g. a move, which only changes path) keeps its id.
    photo_id = existing["photo_id"] if existing and existing["photo_id"] else str(uuid.uuid4())
    conn.execute(
        "INSERT INTO photos (content_hash, path, favorite, photo_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(content_hash) DO UPDATE SET path = excluded.path, favorite = excluded.favorite",
        (content_hash, str(path), int(favorite), photo_id),
    )
    conn.commit()
    # Refresh the export if this photo is (or was) favorited, so a move that
    # relocates a favorited photo keeps stars.json's path field accurate,
    # not just its favorite set membership.
    was_favorite = existing["favorite"] if existing else 0
    if favorite or was_favorite:
        export_stars_json(conn)


def set_favorite(conn, content_hash, favorite):
    conn.execute(
        "UPDATE photos SET favorite = ? WHERE content_hash = ?",
        (int(favorite), content_hash),
    )
    conn.commit()
    export_stars_json(conn)


def export_stars_json(conn):
    """
    Refreshes the human-readable, tool-agnostic favorites backup. Keyed by
    content hash (not path) so it stays correct across renames/moves done
    through the app - those update appdb's path column but never the hash,
    so this export doesn't need to run on every move, only on favorite
    changes.
    """
    rows = conn.execute(
        "SELECT content_hash, path, favorite FROM photos WHERE favorite = 1"
    ).fetchall()
    path = conn.library.stars_json_path
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        row["content_hash"]: {"path": row["path"], "favorite": True}
        for row in rows
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def get_photo_by_hash(conn, content_hash):
    return conn.execute(
        "SELECT * FROM photos WHERE content_hash = ?", (content_hash,)
    ).fetchone()


def get_photo_by_path(conn, path):
    return conn.execute(
        "SELECT * FROM photos WHERE path = ?", (str(path),)
    ).fetchone()


def all_favorited_under(conn, path_prefix):
    rows = conn.execute(
        "SELECT * FROM photos WHERE favorite = 1 AND path LIKE ?",
        (f"{path_prefix}%",),
    ).fetchall()
    return rows


def upsert_moment(conn, moment_id, path, label):
    conn.execute(
        "INSERT INTO moments (moment_id, path, label) VALUES (?, ?, ?) "
        "ON CONFLICT(moment_id) DO UPDATE SET path = excluded.path, label = excluded.label",
        (moment_id, str(path), label),
    )
    conn.commit()


def get_moment_by_path(conn, path):
    return conn.execute(
        "SELECT * FROM moments WHERE path = ?", (str(path),)
    ).fetchone()


def record_read_failure(conn, path, error):
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    conn.execute(
        "INSERT INTO failed_reads (path, error, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET error = excluded.error, last_seen_at = excluded.last_seen_at",
        (str(path), error, now, now),
    )
    conn.commit()


def clear_read_failure(conn, path):
    conn.execute("DELETE FROM failed_reads WHERE path = ?", (str(path),))
    conn.commit()


def list_read_failures(conn):
    return conn.execute(
        "SELECT * FROM failed_reads ORDER BY last_seen_at DESC"
    ).fetchall()
