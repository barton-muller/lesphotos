"""
Pre-assembled zip archives of thumbnails, so a bulk "download all" from the
client costs one sequential read per SHARD_SIZE photos instead of one
scattered-file disk seek per photo - see docs/decisions/thumb-shards.md.

Assignment is by DERIVATION order, not capture date: derive_pending
processes its backlog newest-first (see indexer.py), so keying shards by
taken_at would mean one derive run touches shards spanning the whole
library instead of just appending to the current tail. A photo is
assigned to a shard exactly once, the moment it's derived, and never
moves - not on a later re-derive, not when its moment folder is renamed
or merged (shard membership only ever reads content_hash/photo_id, never
path).

Exactly one shard is ever OPEN (still being appended to) at a time. It
freezes - permanently, its zip never touched again - the moment it
reaches SHARD_SIZE members. A frozen shard's zip is assembled once,
incrementally (zipfile append mode), never rebuilt from scratch.
"""

import sqlite3
import zipfile
from pathlib import Path

# Not measured against the actual NAS - see the design conversation.
# 500 photos * ~20KB thumbnail =~ 10MB transient zip, trivial against the
# DS124's 968MB even un-tuned; revisit with a real on-NAS measurement if
# that assumption turns out wrong.
SHARD_SIZE = 500


def _shard_zip_path(library, shard: int) -> Path:
    return library.thumb_shards_dir / f"{shard}.zip"


def _current_open_shard(conn: sqlite3.Connection) -> int:
    """
    The highest-numbered non-frozen shard, or a freshly created one
    (MAX(shard) + 1, so this is correct both on a brand-new library AND
    after every existing shard has frozen - a bug caught in testing:
    hardcoding shard 0 here worked for the first shard but collided with
    an already-frozen shard 0 the moment a second shard needed creating).
    """
    row = conn.execute(
        "SELECT shard FROM thumb_shards WHERE frozen = 0 ORDER BY shard DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["shard"]
    next_shard = conn.execute("SELECT COALESCE(MAX(shard) + 1, 0) AS n FROM thumb_shards").fetchone()["n"]
    conn.execute("INSERT INTO thumb_shards (shard, frozen, member_count) VALUES (?, 0, 0)", (next_shard,))
    return next_shard


def assign_and_append(conn: sqlite3.Connection, library, content_hash: str, photo_id: str, thumb_path: Path) -> None:
    """
    Called once per photo, right after derive_pending successfully builds
    its thumb.webp (see indexer.py). Assigns the photo to the current open
    shard, appends its thumbnail into that shard's zip (append mode - see
    module docstring - so this never re-reads/re-writes earlier members),
    and freezes the shard if it just reached SHARD_SIZE.

    A photo that already has a shard_members row (a re-derive - see module
    docstring) is left alone: its original shard assignment stands, and
    its zip entry is intentionally NOT refreshed even though the on-disk
    thumb.webp just changed. Accepted trade, not an oversight - see
    docs/decisions/thumb-shards.md's re-derive scenario for why.
    """
    existing = conn.execute(
        "SELECT shard FROM thumb_shard_members WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing:
        return

    shard = _current_open_shard(conn)
    zip_path = _shard_zip_path(library, shard)

    # The DB row is committed in batches by the caller (derive_pending's
    # COMMIT_BATCH), not after every single photo - a crash between this
    # zip write and that commit would otherwise re-run this photo next
    # time (its shard_members row never landed) and try to append the
    # SAME photo_id into the zip a second time. zipfile allows duplicate
    # entry names (later one wins on read in most tools, but it's still
    # wasted space and a warning) rather than rejecting them, so the
    # namelist check here is what actually prevents it - confirmed by
    # reproducing exactly this double-write in testing (an interrupted-DB
    # -but-not-zip scenario) before this check was added.
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_STORED) as zf:
        if photo_id not in zf.namelist():
            zf.write(thumb_path, arcname=photo_id)

    conn.execute(
        "INSERT INTO thumb_shard_members (content_hash, shard, photo_id) VALUES (?, ?, ?)",
        (content_hash, shard, photo_id),
    )
    new_count = conn.execute(
        "SELECT member_count FROM thumb_shards WHERE shard = ?", (shard,)
    ).fetchone()["member_count"] + 1
    frozen = 1 if new_count >= SHARD_SIZE else 0
    conn.execute(
        "UPDATE thumb_shards SET member_count = ?, frozen = ? WHERE shard = ?",
        (new_count, frozen, shard),
    )


def list_shards(conn: sqlite3.Connection) -> list[dict]:
    """[{shard, frozen, member_count}, ...] for the client's manifest endpoint."""
    return [dict(row) for row in conn.execute("SELECT shard, frozen, member_count FROM thumb_shards ORDER BY shard")]


def backfill(conn: sqlite3.Connection, library, verbose: bool = False) -> dict:
    """
    One-time catch-up for a library that had photos derived BEFORE the
    shard system existed: derive_pending only ever selects derived = 0
    rows, so an already-derived photo would otherwise never get a shard
    assignment - confirmed on a real 26,498-photo library where 25,898
    photos had derived = 1 but zero thumb_shard_members rows.

    Walks every derived photo with no shard assignment yet, oldest
    derivation first as a proxy (their real derivation order is lost -
    only `taken_at` survived) - see thumb-shards.md for why capture-date
    order is an accepted approximation ONLY for this one-time backfill,
    never for the ongoing per-derive assignment in assign_and_append,
    where the real derivation order is available and used.

    Safe to interrupt and re-run: each photo's assignment is committed in
    COMMIT_BATCH-sized groups, same as derive_pending, and a photo already
    holding a thumb_shard_members row is skipped by the query itself.
    """
    rows = conn.execute(
        "SELECT p.content_hash, p.photo_id, m.taken_at "
        "FROM photos p JOIN media m ON m.content_hash = p.content_hash "
        "LEFT JOIN thumb_shard_members s ON s.content_hash = p.content_hash "
        "WHERE m.derived = 1 AND s.content_hash IS NULL "
        "ORDER BY m.taken_at ASC"
    ).fetchall()

    done = skipped = 0
    for i, r in enumerate(rows):
        thumb_path = library.cache_root / r["photo_id"] / "thumb.webp"
        if not thumb_path.is_file():
            skipped += 1
            continue
        assign_and_append(conn, library, r["content_hash"], r["photo_id"], thumb_path)
        done += 1
        if done % 500 == 0:
            conn.commit()
            if verbose:
                print(f"  ...{done}/{len(rows)} backfilled")
    conn.commit()
    return {"backfilled": done, "skipped_no_thumb": skipped, "total_candidates": len(rows)}
