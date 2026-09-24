#!/usr/bin/env python3
"""
The HTTP API the browser client talks to.

Auth model: every user has their own account and their own photo library
(see auth.py, config.py). A signed cookie carries the user id; the
`current` dependency turns that into a Library, and every endpoint takes
its paths and its database from that object.

There is no admin and no cross-user access, by construction rather than
by permission check: an endpoint has no way to name a library other than
the caller's own, because the only Library it can reach is the one the
dependency built from their token.
"""

import io
import logging
import os
import re
import time
import zipfile
from datetime import timedelta
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

log = logging.getLogger(__name__)
from pydantic import BaseModel

import auth
import config
import geocoding
import indexer
import moment_dates
import platform_hosts
import reorganize
import thumb_shards
from appdb import db
from moment_dates import date_prefix_str, parse_moment_date_prefix

app = FastAPI()

# In production the API and the built client are served by this same
# process (see the static mount at the end of this file), so requests are
# same-origin and no CORS headers are needed at all. This exists only for
# `vite dev` on a different port, hence the env var - hardcoding
# localhost:5173 shipped a dev origin into production.
_CORS_ORIGINS = [o for o in os.environ.get("LESPHOTOS_CORS_ORIGINS", "").split(",") if o]
if _CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

class Session:
    """The caller, and the one library they are allowed to touch."""

    def __init__(self, user, library: config.Library):
        self.user = user
        self.library = library

    @property
    def name(self) -> str:
        return self.user["name"]


def current(auth_token: str | None = Cookie(default=None, alias="auth-token")) -> Session:
    """
    Turns the session cookie into a Session, or 401s.

    This is the only way an endpoint gets a Library, which is what makes
    cross-user access structurally impossible rather than merely
    forbidden: there is no parameter anywhere that names a different
    user's library, so there is no check to forget and no way to ask.
    """
    user_id = auth.parse_token(_AUTH_SECRET, auth_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="not logged in")
    conn = auth.connect()
    try:
        user = auth.get_user_by_id(conn, user_id)
    finally:
        conn.close()
    # A correctly-signed token for a user who has since been deleted.
    if user is None:
        raise HTTPException(status_code=401, detail="not logged in")
    return Session(user, auth.library_for(user))


def _resolve_host_path(library: config.Library, path: str) -> Path:
    """
    Turns a client-supplied library-relative path into an absolute one,
    rejecting anything that escapes the library (invariant #5).

    Takes the library explicitly. It used to read a module-level
    sess.library.photos_root, which was safe only while there was one
    library in the world - with per-user roots, a path check that reads a
    shared global is not a path check at all.
    """
    root = library.photos_root
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=400, detail="invalid path")
    return resolved


@app.get("/health")
def health():
    return {"ok": True}


class FavoriteRequest(BaseModel):
    path: str
    favorite: bool


@app.post("/favorite")
def favorite(req: FavoriteRequest, sess: Session = Depends(current)):
    conn = db.connect(sess.library)
    host_path = _resolve_host_path(sess.library, req.path)
    reorganize.set_favorite(conn, host_path, req.favorite)
    return {"ok": True}


class RenameMomentRequest(BaseModel):
    moment_path: str
    new_label: str
    new_date: str | None = None


def _run_reorganize(fn, *args, **kwargs):
    """
    Shared error handling for reorganize.py calls that can legitimately hit
    filesystem drift (the caller's cached photo/moment list vs. the real
    disk state) - confirmed in practice, not hypothetical: an unguarded
    move crashed with an uncaught FileNotFoundError and an opaque 500.
    Surfaces a clear 409 instead. A bad new_date is the caller's mistake,
    not drift, so it gets its own clean 400 rather than falling through to
    an opaque 500.
    """
    try:
        return fn(*args, **kwargs)
    except (FileNotFoundError, FileExistsError) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/moments/rename")
def rename_moment(req: RenameMomentRequest, sess: Session = Depends(current)):
    host_path = _resolve_host_path(sess.library, req.moment_path)
    new_path = _run_reorganize(
        reorganize.rename_moment, sess.library, host_path, req.new_label, req.new_date
    )
    return {"ok": True, "new_path": str(new_path.relative_to(sess.library.photos_root))}


class MergeMomentsRequest(BaseModel):
    moment_paths: list[str]
    new_label: str


@app.post("/moments/merge")
def merge_moments(req: MergeMomentsRequest, sess: Session = Depends(current)):
    host_paths = [_resolve_host_path(sess.library, p) for p in req.moment_paths]
    dest = _run_reorganize(
        reorganize.merge_moments, sess.library, host_paths, req.new_label
    )
    return {"ok": True, "new_path": str(dest.relative_to(sess.library.photos_root))}


class CreateMomentRequest(BaseModel):
    photo_paths: list[str]
    new_label: str


@app.post("/moments/create")
def create_moment(req: CreateMomentRequest, sess: Session = Depends(current)):
    """
    User-selected range/set of photos -> a new moment folder. Thin wrapper
    around reorganize.move_photos, which already creates its destination
    folder if it doesn't exist - see docs/decisions/range-select-new-moment.md
    for why no server-side logic change was needed beyond this endpoint.
    """
    if not req.photo_paths:
        raise HTTPException(status_code=400, detail="no photos selected")

    host_paths = [_resolve_host_path(sess.library, p) for p in req.photo_paths]
    # Earliest moment (by its own YYYY-MM-DD-prefixed folder name, which
    # sorts chronologically) supplies the date prefix, matching
    # merge_moments' "earliest date wins" convention. Sorting by the
    # *photo's own filename* here was a bug fixed in the same pass as the
    # FileNotFoundError below - filenames aren't reliably date-ordered
    # across different moments, only each moment's own folder name is.
    earliest_parent = min((p.parent for p in host_paths), key=lambda d: d.name)
    date_prefix = date_prefix_str(earliest_parent.name)
    dest_dir = earliest_parent.parent / f"{date_prefix}-{req.new_label}"

    # No scan_pending case any more: that existed because PhotoView's
    # global scanAll could outlast its wait window after the moves had
    # already landed. The move now completes synchronously in bounded time.
    moved = _run_reorganize(
        reorganize.move_photos, sess.library, host_paths, dest_dir
    )

    return {
        "ok": True,
        "new_path": str(dest_dir.relative_to(sess.library.photos_root)),
        "moved_count": len(moved),
        "scan_pending": False,
    }


class DeletePhotosRequest(BaseModel):
    photo_paths: list[str]


@app.post("/photos/delete")
def delete_photos(req: DeletePhotosRequest, sess: Session = Depends(current)):
    """
    Moves selected photos to library/trash/YYYY/YYYY-MM-DD/ - a real,
    walked moment folder (see docs/decisions/delta-sync.md), not a hidden
    sidecar path. That matters: .lesphotos/ is skipped by the indexer's own
    walk (it has to be, or the indexer would try to index its own cache),
    so anything living there is invisible to a full re-index and would be
    silently lost forever on a rebuilt appdb. A plain top-level folder
    keeps trashed photos exactly as rebuildable as everything else -
    invariant #1 - and makes "restore" trivial later: move the file back,
    same as moving it anywhere else.

    Today's date, not each photo's own, so repeated deletes land in
    different folders and a future GC sweep can read "how old is this
    batch" straight from the folder name. Reuses move_photos - the same
    validated-move primitive rename/merge/create-moment already share - so
    a Live Photo's clip moves with its still, motion_path gets repaired,
    and a stale client-supplied path still gets a clean 409 instead of a
    crash. No new filesystem logic; a delete is just a move to a
    known destination.
    """
    if not req.photo_paths:
        raise HTTPException(status_code=400, detail="no photos selected")

    host_paths = [_resolve_host_path(sess.library, p) for p in req.photo_paths]
    today = time.strftime("%Y-%m-%d")
    dest_dir = sess.library.photos_root / "trash" / today[:4] / today

    moved = _run_reorganize(
        reorganize.move_photos, sess.library, host_paths, dest_dir
    )

    return {
        "ok": True,
        "new_path": str(dest_dir.relative_to(sess.library.photos_root)),
        "moved_count": len(moved),
    }


class SuggestNameRequest(BaseModel):
    moment_path: str


@app.post("/moments/suggest-name")
def suggest_name(req: SuggestNameRequest, sess: Session = Depends(current)):
    """
    Opt-in only - never called automatically during scan/reorganize (see
    docs/decisions/geo-reverse-geocoding.md).

    Coarse city-level naming (geocoding.suggest_city_moment_label), not
    Nominatim's street-level suggest_moment_label - the latter is right for
    geocoding.py's original use (a single "where was I" lookup) but was too
    noisy for a moment name a human picks at a glance (see
    docs/known-issues.md's resolved "overly specific geopoint name" entry).
    This matches the coarser naming tools/name_moments_by_city.py already
    used for the Takeout import, including its custom_locations.json
    overrides - no Nominatim call, no network, no 1 req/sec wait.
    """
    host_path = _resolve_host_path(sess.library, req.moment_path)
    if not host_path.is_dir():
        raise HTTPException(status_code=404, detail="moment not found")

    gps_list = [geocoding.read_gps(f) for f in host_path.iterdir() if f.is_file()]
    label = geocoding.suggest_city_moment_label(gps_list)
    if not label:
        return {"ok": True, "suggestion": None}
    date_prefix = date_prefix_str(host_path.name)
    return {"ok": True, "suggestion": f"{date_prefix}-{label}"}


@app.get("/photo-ids")
def photo_ids(response: Response, sess: Session = Depends(current)):
    """
    Stable per-photo ids, decoupled from PhotoView's own path-derived id
    (see docs/decisions/stable-photo-id.md) - the client's sync fetches this
    once per sync pass and resolves each PhotoView timeline entry's stable
    id by matching path, instead of keying its IndexedDB cache on
    PhotoView's own id (which changes on every move/rename).

    Keyed in PhotoView's container-relative path form (e.g.
    "/photos/alice/...") since that's the same form the client already gets
    back from myTimeline's `path` field - the client never sees appdb's
    host-absolute paths directly.

    Explicit no-store: this data changes on every reorganize call, but
    without this header the browser's default heuristic caching can (and,
    confirmed via direct user testing, does) silently serve a stale GET
    response with no error - "created a moment, synced with no errors, but
    it's still not visible" traced back to exactly this. The client's own
    fetch call also passes cache: 'no-store' - belt and suspenders, since
    relying on only one side leaves any other caller (or a future service
    worker route) vulnerable to the same silent staleness.
    """
    response.headers["Cache-Control"] = "no-store"
    conn = reorganize.ensure_all_photos_registered(sess.library)
    rows = conn.execute("SELECT path, photo_id FROM photos WHERE photo_id IS NOT NULL").fetchall()
    return {reorganize.client_path(sess.library, row["path"]): row["photo_id"] for row in rows}


class AutoGenerateMomentsRequest(BaseModel):
    source_dir: str
    dry_run: bool = False
    # Days, matching initial_script.py's EVENT_GAP unit - exposed as a
    # request field (not hardcoded) since the right gap is a judgment call
    # that can reasonably differ per library, not a fixed constant.
    gap_days: float = 2.0


@app.post("/moments/auto-generate")
def auto_generate_moments(
    req: AutoGenerateMomentsRequest, sess: Session = Depends(current),
):
    """
    Regroups a flat/messy photo tree into gap-based moment folders - see
    docs/decisions/auto-generate-moments.md and
    reorganize.auto_generate_moments's own docstring for the full design.
    dry_run=True returns the computed plan (destination folder names + how
    many photos each would get) without touching the filesystem, so the
    client can show a preview before committing - this can move a lot of
    files at once, unlike every other reorganize endpoint which only ever
    acts on a small, explicitly-selected set.
    """
    host_path = _resolve_host_path(sess.library, req.source_dir)
    gap = timedelta(days=req.gap_days)

    plan = _run_reorganize(
        reorganize.auto_generate_moments,
        sess.library,
        host_path,
        gap=gap,
        dry_run=req.dry_run,
    )
    return {
        "ok": True,
        "scan_pending": False,
        "moments": [
            {"dest": str(Path(p["dest"]).relative_to(sess.library.photos_root)), "count": p["count"]}
            for p in plan
        ],
    }


# ---------------------------------------------------------------------------
# First-party media serving - replaces PhotoView's myTimeline + thumbnail
# endpoints. See docs/decisions/replacing-photoview.md.
# ---------------------------------------------------------------------------


def _moment_of(library: config.Library, path: str) -> tuple[str, str]:
    """
    (label, moment_path) for a photo, derived from its parent directory.

    Moments ARE directories in this design (invariant #3), so this needs no
    stored state and no join - the filesystem already holds the answer.
    PhotoView had to model albums as rows; we don't.

    The label is the parent's own name EXCEPT for a raw phone-backup
    layout's bare month folder ("09"), which is combined with its parent
    year folder into "2024-09" instead - a bare "09" is identical across
    every year, so left alone it doesn't just sort wrong (moment_date
    fixes that), it also LOOKS identical for every September the library
    has ever had.
    """
    parent = Path(path).parent
    try:
        rel = parent.relative_to(library.photos_root)
    except ValueError:
        rel = parent
    label = parent.name
    if moment_dates.is_month_bucket(parent.name, parent.parent.name) and not moment_dates.has_date_prefix(parent.name):
        label = f"{parent.parent.name}-{parent.name}"
    return label, str(rel)


@app.get("/timeline/count")
def timeline_count(sess: Session = Depends(current)):
    """
    How many photos are visible in the timeline, cheaply.

    Superseded by /timeline/max-seq for sync.ts's own short-circuit check
    (see that endpoint's docstring for why), but kept as a cheap read for
    any other caller that only cares about the display count.
    """
    conn = db.connect(sess.library)
    n = conn.execute(
        "SELECT COUNT(*) c FROM media WHERE derived = 1 AND is_motion_clip = 0"
    ).fetchone()["c"]
    conn.close()
    return {"count": n}


@app.get("/timeline/max-seq")
def timeline_max_seq(sess: Session = Depends(current)):
    """
    The highest changed_seq in the library, cheaply - what a delta sync
    compares against its own last-seen value to decide whether anything
    changed at all before paying for a /timeline?since= fetch.

    Replaces /timeline/count as sync.ts's short-circuit check: count only
    catches additions/deletions, silently missing a favorite toggle or a
    rename with no count change (see docs/decisions/delta-sync.md) - those
    used to wait for "Sync now" or a lucky full relist. changed_seq is
    bumped by every write that should be visible to another device (see
    db.bump_changed_seq's callers), so this one number covers all of them.
    """
    conn = db.connect(sess.library)
    n = conn.execute("SELECT COALESCE(MAX(changed_seq), 0) c FROM media").fetchone()["c"]
    conn.close()
    return {"maxSeq": n}


@app.get("/timeline")
def timeline(
    response: Response,
    limit: int = Query(default=500, le=2000),
    offset: int = Query(default=0, ge=0),
    since: int | None = Query(default=None, ge=0),
    sess: Session = Depends(current),
):
    """
    One page of the timeline, newest first - or, with `since`, a delta feed
    of every row touched after that changed_seq (see
    docs/decisions/delta-sync.md), which is what makes a routine sync O(what
    changed) instead of O(library size).

    Deliberately paginated with a hard ceiling rather than offering a
    "give me everything" mode: at the ~5TB target a full-library response
    would be a large JSON serialisation held entirely in RAM on a 1GB box.
    The client pages until it gets a short page, the same shape its
    PhotoView fetchTimeline() loop already used.

    Ordered by the MOMENT's date - the date parsed from the containing
    directory's own name (see moment_dates.py) - and only then by capture
    time within that moment. `since` mode instead orders by changed_seq
    (a change feed, not a display order) - moment-date order has no
    meaning across a set of rows that may span the whole library's history.

    A moment IS a directory (invariant #3) and its name is what the user sees
    and renames, so the name has to be what decides where it appears. Ordering
    by taken_at alone let any photo whose capture time disagreed with its
    folder - an old import, a camera with a wrong clock, a scan - scatter
    itself up and down the timeline away from the moment it belongs to.

    The moment date used to be derived from the path in SQL, on every query -
    a ~20-line nested rtrim/replace/substr expression, since SQLite has no
    dirname/basename. It's now media.moment_date, computed once at index
    time (or on move) by the same parser every other moment-date call site
    uses, and kept in sync there, not read fresh here - the filesystem is
    still the source of truth, this is just a cache of it, exactly like
    every other column in appdb. Sorts on a plain indexed column instead of
    a full per-row string derivation.

    Trash (library/trash/YYYY/YYYY-MM-DD/ - see /photos/delete) is excluded
    from the default (no `since`) listing by path prefix, same as any other
    moment would be included: it's a real, indexed moment, just one the
    everyday grid doesn't show. `since` mode does NOT exclude it - a
    trashed photo is a hidden moment, not a deletion, so a delta client
    sees it arrive with its new trash path like any other move and can
    choose to stop showing it (see sync.ts), the same way the default
    listing does server-side. This is what lets a future "view trash"
    toggle be "stop filtering," not a new endpoint.

    no-store for the same reason as /photo-ids: this changes on every
    reorganize, and a silently-cached GET here would resurrect exactly the
    "created a moment, synced fine, still not visible" class of bug.
    """
    response.headers["Cache-Control"] = "no-store"
    conn = db.connect(sess.library)
    trash_prefix = str(sess.library.photos_root / "trash") + os.sep
    if since is not None:
        rows = conn.execute(
            "SELECT p.photo_id, p.path, p.favorite, m.taken_at, m.width, m.height, m.motion_kind, "
            "       m.is_video, m.duration, m.moment_date, m.changed_seq "
            "FROM media m JOIN photos p ON p.content_hash = m.content_hash "
            "WHERE m.changed_seq > ? AND m.is_motion_clip = 0 "
            "ORDER BY m.changed_seq ASC "
            "LIMIT ?",
            (since, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT p.photo_id, p.path, p.favorite, m.taken_at, m.width, m.height, m.motion_kind, "
            "       m.is_video, m.duration, m.moment_date, m.changed_seq "
            "FROM media m JOIN photos p ON p.content_hash = m.content_hash "
            "WHERE m.derived = 1 AND m.is_motion_clip = 0 AND p.path NOT LIKE ? || '%' "
            # photo_id last as a tiebreaker: LIMIT/OFFSET paging is only
            # stable if the sort is total, and photos sharing a moment date
            # AND a taken_at (a burst, or anything falling back to the same
            # file mtime) are otherwise free to order differently between one
            # page query and the next - which drops or duplicates rows at the
            # page boundary. moment_date NULL (no parseable prefix) sorts last
            # under DESC, same as the old ''-sorts-last behaviour.
            "ORDER BY m.moment_date DESC, m.taken_at DESC, p.photo_id "
            "LIMIT ? OFFSET ?",
            (trash_prefix, limit, offset),
        ).fetchall()
    out = []
    for r in rows:
        # A row whose path is outside PHOTOS_ROOT can't be expressed as a
        # library-relative path, and _photoview_path raises on it. That used to
        # take the whole page down with a 500 - one stray row made every
        # request covering its offset fail, so the client's paginated sync
        # stopped dead partway through the library. Skipping the row degrades
        # to "that photo isn't listed", which is both true and recoverable.
        try:
            rel_path = reorganize.client_path(sess.library, r["path"])
        except ValueError:
            log.warning("timeline: skipping out-of-root path %s", r["path"])
            continue
        label, moment_path = _moment_of(sess.library, r["path"])
        out.append({
            "id": r["photo_id"],
            "path": rel_path,
            "moment": label,
            "momentPath": moment_path,
            # The server's own real sort key (see this endpoint's ORDER
            # BY) - NOT derivable from `moment` alone for a month-only
            # bucket folder like "2026-09", whose moment_date tracks its
            # newest photo rather than defaulting to day=1 of the month
            # (see indexer.rebalance_month_bucket_dates). A client sorting
            # by parsing `moment` itself will get that case wrong; this
            # field is the fix.
            "momentDate": r["moment_date"],
            "date": r["taken_at"],
            "favorite": bool(r["favorite"]),
            "width": r["width"],
            "height": r["height"],
            "isVideo": bool(r["is_video"]),
            "duration": r["duration"],
            # A still with a clip attached - Live Photo or Motion Photo.
            # The client only needs to know it exists; where the pixels
            # come from is /media/{id}/motion's problem.
            "hasMotion": r["motion_kind"] is not None,
        })
        if since is not None:
            out[-1]["changedSeq"] = r["changed_seq"]
    if since is not None:
        # Wrapped, not a bare array, so a client can tell "delta response"
        # apart from the plain paginated shape without guessing from
        # request params alone.
        #
        # max_seq here is the LAST ROW IN THIS PAGE's changed_seq, not the
        # library's global max - critical when `limit` truncates the page:
        # if the client advanced its cursor to the global max after only
        # seeing a partial page, every row between this page's end and the
        # global max would be silently skipped forever (they're all
        # `changed_seq <= since` on the client's next call, so they'd never
        # be asked for again). An empty page (nothing new) has no last row
        # to take this from, so it falls back to `since` unchanged - correct,
        # since nothing advanced.
        max_seq = out[-1]["changedSeq"] if out else since
        conn.close()
        return {"items": out, "maxSeq": max_seq}
    conn.close()
    return out


def _derivative(sess: "Session", photo_id: str, name: str) -> FileResponse:
    """
    Serves one derivative from the caller's own cache.

    photo_id is validated as a bare UUID rather than being trusted as a path
    component - it arrives from the client, and invariant #6 says no
    client-supplied value reaches the filesystem unvalidated. A UUID has no
    legitimate reason to contain a separator, so anything that does is
    rejected outright instead of being normalised.

    Derivatives are immutable for a given photo_id (a re-derive writes the
    same content for the same id), so they get a long max-age - this is the
    request type that dominates browsing volume, and the cheapest version of
    it is the one the browser never sends. "private", though: with per-user
    libraries these are one person's photos, and must never be held in a
    shared proxy cache.

    These two endpoints previously took no auth at all, so anyone who
    could reach the port could pull any thumbnail by guessing a UUID. That
    was survivable with one shared password and one library; with four
    users it is a cross-user leak, so the cache root now comes from the
    session rather than a global.
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", photo_id):
        raise HTTPException(status_code=400, detail="invalid id")
    f = sess.library.cache_root / photo_id / name
    if not f.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        f,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.get("/media/{photo_id}/thumb")
def media_thumb(photo_id: str, sess: Session = Depends(current)):
    return _derivative(sess, photo_id, "thumb.webp")


class ThumbBatchRequest(BaseModel):
    photo_ids: list[str]


# One HTTP request per thumbnail (26,000+ for a full "download all") pays
# fixed per-request overhead - headers, the current dependency's cookie
# parse, FastAPI's own routing - on top of a ~20KB payload, and the
# browser caps concurrent connections to 6 per origin on HTTP/1.1
# (getting HTTP/2 would need TLS, which this deploy doesn't have and
# isn't worth adding just for this). Batching many thumbnails into one
# zip response amortizes that overhead across MAX_BATCH photos instead
# of paying it per photo. ZIP_STORED, not DEFLATE: webp is already
# compressed, so deflating it again just burns CPU (real concern on the
# DS124's ARM cores) for no size win.
MAX_THUMB_BATCH = 300


@app.post("/media/thumbs-batch")
def media_thumbs_batch(req: ThumbBatchRequest, sess: Session = Depends(current)):
    """
    Zip of thumb.webp for every requested photo_id that exists in this
    session's library. A missing/invalid id is silently skipped rather
    than failing the whole batch - the client already treats "no blob
    yet" as a per-photo state, so a partial batch degrades the same way
    a partial set of individual /thumb 404s would, just without needing
    300 separate response codes to express it.

    Entirely in-memory (io.BytesIO): measured a real 300-thumbnail batch
    at ~6MB and 3ms to assemble - trivial next to the DS124's 1GB even
    with a couple of these in flight at once, and far simpler than a
    true incrementally-streamed zip for a payload this small.
    """
    if len(req.photo_ids) > MAX_THUMB_BATCH:
        raise HTTPException(status_code=400, detail=f"max {MAX_THUMB_BATCH} ids per batch")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for photo_id in req.photo_ids:
            # Same UUID check as _derivative (invariant #6: no
            # client-supplied value reaches the filesystem unvalidated),
            # duplicated rather than reused because _derivative raises on
            # a bad id and this endpoint skips-and-continues instead -
            # one bad id in a 300-id batch must not 400 the other 299.
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", photo_id):
                continue
            f = sess.library.cache_root / photo_id / "thumb.webp"
            if not f.is_file():
                continue
            zf.write(f, arcname=photo_id)

    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/media/thumb-shards")
def thumb_shards_manifest(sess: Session = Depends(current)):
    """
    Which shards exist and whether each is frozen (see thumb_shards.py).
    The client fetches a frozen shard's zip once (its content is
    permanent) and falls back to /media/thumbs-batch for whatever the
    still-open shard - and any earlier shard this session shouldn't
    trust as final yet - doesn't cover.
    """
    conn = db.connect(sess.library)
    return {"shards": thumb_shards.list_shards(conn)}


@app.get("/media/thumb-shards/{shard}")
def thumb_shard_zip(shard: int, sess: Session = Depends(current)):
    """
    The pre-assembled zip for one FROZEN shard. Refuses to serve an open
    shard - it's still being appended to (see thumb_shards.assign_and_append),
    so its zip on disk may not reflect every photo the manifest just
    promised was in this shard. The client's fallback to /media/thumbs-batch
    for a not-yet-frozen shard already covers this case; this endpoint
    only needs to handle the frozen, permanent, safe-to-cache-forever ones.
    """
    conn = db.connect(sess.library)
    row = conn.execute("SELECT frozen FROM thumb_shards WHERE shard = ?", (shard,)).fetchone()
    if not row or not row["frozen"]:
        raise HTTPException(status_code=404, detail="not a frozen shard")
    f = sess.library.thumb_shards_dir / f"{shard}.zip"
    if not f.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        f,
        media_type="application/zip",
        # Frozen means frozen: this exact shard number will never contain
        # different bytes, unlike the derivative endpoints above which are
        # "private" for a per-user-privacy reason but still content-stable
        # per id - same reasoning applies here, just at shard granularity.
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.get("/media/{photo_id}/preview")
def media_preview(photo_id: str, sess: Session = Depends(current)):
    return _derivative(sess, photo_id, "preview.webp")


@app.get("/media/{photo_id}/original")
def media_original(photo_id: str, sess: Session = Depends(current)):
    """
    The original file, for full-res viewing and video playback.

    FileResponse handles HTTP range requests, which is what makes <video>
    seeking work without us implementing byte ranges by hand - and it uses
    sendfile(), so the bytes go kernel-to-socket without passing through
    Python. That is the whole reason video playback costs this server
    almost nothing despite the files being large.
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", photo_id):
        raise HTTPException(status_code=400, detail="invalid id")
    conn = db.connect(sess.library)
    row = conn.execute("SELECT path FROM photos WHERE photo_id = ?", (photo_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    f = Path(row["path"])
    # The stored path came from our own scan, but re-validate anyway: appdb
    # is a file on disk and this is the one place a bad row would turn into
    # arbitrary filesystem read access.
    if not f.is_file() or not f.resolve().is_relative_to(sess.library.photos_root):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(f, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/media/{photo_id}/compat")
def media_compat(photo_id: str, sess: Session = Depends(current)):
    """
    An H.264 version of a video, for browsers that cannot decode the
    original (~95% of the library is HEVC: Safari/iOS play it, Firefox
    does not).

    Deliberately a separate endpoint rather than content negotiation on
    /original: the client knows whether playback failed, the server would
    have to guess from User-Agent, and guessing wrong means serving a
    lower-resolution file to the majority who did not need it. So
    /original stays the default and the client asks for this only after
    the original fails to play.

    404s when the host has no transcode - coverage is not total - which
    the client treats as "this video cannot play here" rather than an
    error worth reporting.
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", photo_id):
        raise HTTPException(status_code=400, detail="invalid id")
    conn = db.connect(sess.library)
    row = conn.execute(
        "SELECT p.path FROM photos p JOIN media m ON m.content_hash = p.content_hash "
        "WHERE p.photo_id = ? AND m.is_video = 1",
        (photo_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    f = Path(row["path"])
    if not f.is_file() or not f.resolve().is_relative_to(sess.library.photos_root):
        raise HTTPException(status_code=404, detail="not found")

    compat = platform_hosts.existing_compat_video(f)
    # Re-validate: this path is built from the library path above, but it
    # is still a filesystem read and the same rule applies (invariant #5).
    if not compat or not compat.resolve().is_relative_to(sess.library.photos_root):
        raise HTTPException(status_code=404, detail="no compatible version")
    return FileResponse(
        compat,
        media_type="video/mp4",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.get("/media/{photo_id}/motion")
def media_motion(photo_id: str, sess: Session = Depends(current)):
    """
    The video half of a Live Photo / Motion Photo.

    Two storage shapes behind one URL (see
    docs/decisions/live-and-motion-photos.md):

      apple  - a sibling .MOV, served as a file
      google - an MP4 appended inside the JPEG, served as a byte range
               from the offset recorded at index time

    Neither decodes anything, which is what lets this work on a NAS with
    no video decoder at all.
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", photo_id):
        raise HTTPException(status_code=400, detail="invalid id")
    conn = db.connect(sess.library)
    row = conn.execute(
        "SELECT p.path, m.motion_kind, m.motion_path, m.motion_offset "
        "FROM photos p JOIN media m ON m.content_hash = p.content_hash "
        "WHERE p.photo_id = ?",
        (photo_id,),
    ).fetchone()
    conn.close()
    if not row or not row["motion_kind"]:
        raise HTTPException(status_code=404, detail="no motion")

    if row["motion_kind"] == "apple":
        f = Path(row["motion_path"] or "")
        # Same rule as every other path out of appdb (invariant #5): the
        # row was written by our own scan, but it is still a file on disk.
        if not f.is_file() or not f.resolve().is_relative_to(sess.library.photos_root):
            raise HTTPException(status_code=404, detail="no motion")
        return FileResponse(
            f, media_type="video/quicktime",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    still = Path(row["path"])
    if not still.is_file() or not still.resolve().is_relative_to(sess.library.photos_root):
        raise HTTPException(status_code=404, detail="no motion")
    offset = row["motion_offset"]
    if offset is None:
        raise HTTPException(status_code=404, detail="no motion")

    # Read the appended MP4 out of the JPEG. Bounded by the file size and
    # streamed in chunks rather than read whole: these are ~1.5-2MB today,
    # but nothing guarantees that, and the NAS has 1GB of RAM.
    size = still.stat().st_size
    if not 0 < offset < size:
        raise HTTPException(status_code=404, detail="no motion")

    def _chunks():
        with open(still, "rb") as fh:
            fh.seek(offset)
            while chunk := fh.read(256 * 1024):
                yield chunk

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        _chunks(),
        media_type="video/mp4",
        headers={
            "Content-Length": str(size - offset),
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )


@app.post("/index")
def run_index(sess: Session = Depends(current)):
    """
    Triggers an incremental scan.

    Explicitly triggered rather than run on a timer: the server should do
    nothing at idle, which is half the point of replacing PhotoView. An
    unchanged library costs one stat per file and no image work at all
    (measured: 1.26s for 162 files), so this is cheap enough to call after
    a reorganize or from cron.
    """
    stats = indexer.index(sess.library)
    indexer.backfill_moment_dates(sess.library)
    return {"ok": True, **stats}


@app.get("/media/{photo_id}/metadata")
def media_metadata(photo_id: str, sess: Session = Depends(current)):
    """
    EXIF/file detail for one photo, replacing PhotoView's media(id:) query.

    Read from the file on demand rather than stored at index time: the panel
    is opened for a tiny fraction of photos, so indexing full EXIF for the
    whole library would be work done for nothing (the same reasoning that
    made this lazy client-side in the first place - see
    docs/decisions/metadata-panel.md). Only what the panel actually renders
    is extracted, not the whole tag set.
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", photo_id):
        raise HTTPException(status_code=400, detail="invalid id")
    conn = db.connect(sess.library)
    row = conn.execute(
        "SELECT p.path, m.width, m.height, m.is_video, m.duration, m.taken_at "
        "FROM photos p LEFT JOIN media m ON m.content_hash = p.content_hash "
        "WHERE p.photo_id = ?",
        (photo_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    f = Path(row["path"])
    if not f.is_file() or not f.resolve().is_relative_to(sess.library.photos_root):
        raise HTTPException(status_code=404, detail="not found")

    out = {
        "filename": f.name,
        "fileSize": f.stat().st_size,
        "width": row["width"],
        "height": row["height"],
        "isVideo": bool(row["is_video"]),
        "duration": row["duration"],
        "dateShot": row["taken_at"],
        "camera": None, "maker": None, "lens": None,
        "exposure": None, "aperture": None, "iso": None,
        "focalLength": None, "coordinates": None,
    }
    if not row["is_video"]:
        try:
            with Image.open(f) as img:
                exif = img.getexif()
                out["maker"] = exif.get(0x010F)
                out["camera"] = exif.get(0x0110)
                # The photographic tags live in the Exif sub-IFD, not the
                # top-level one - the same gotcha that broke the metadata
                # panel once already (commit a28ee0d).
                sub = exif.get_ifd(0x8769)
                out["lens"] = sub.get(0xA434)
                out["iso"] = sub.get(0x8827)
                for key, tag in (("exposure", 0x829A), ("aperture", 0x829D), ("focalLength", 0x920A)):
                    v = sub.get(tag)
                    out[key] = float(v) if v is not None else None
                gps = geocoding.read_gps(f)
                # read_gps returns a (None, None) tuple - not None - for a
                # photo with no GPS tags, which is truthy. Emitting that as
                # coordinates crashed the client's .toFixed() formatting;
                # check the values, not the tuple.
                if gps and gps[0] is not None and gps[1] is not None:
                    out["coordinates"] = {"latitude": gps[0], "longitude": gps[1]}
        except Exception:
            # A photo with unreadable/absent EXIF still has valid file info
            # worth showing - degrade to that rather than 500ing the panel.
            pass
    return out


# ---------------------------------------------------------------------------
# Auth.
#
# Per-user accounts, each with their own library (auth.py). This replaces
# the previous single shared household password, which could not express
# "these are Alice's photos and those are someone else's" at all.
#
# The token is an HMAC over (user_id, expiry), so the server holds no
# session state and a stolen token expires on its own. Not a substitute
# for putting this behind Tailscale/HTTPS for remote access (see
# docs/decisions/remote-access-quickconnect-vs-tailscale.md).
#
# The signing secret MUST be pinned via LESPHOTOS_SECRET. Without it the
# process invents one at startup, and since a NAS restarts, everyone gets
# logged out - which breaks the "log in once" requirement outright. So
# this refuses to start rather than appearing to work.
# ---------------------------------------------------------------------------

_AUTH_SECRET = auth.load_secret(required=True)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/login")
def login(req: LoginRequest, response: Response):
    conn = auth.connect()
    try:
        user = auth.authenticate(conn, req.username, req.password)
    finally:
        conn.close()
    if user is None:
        # One message for both "no such user" and "wrong password", so this
        # can't be used to enumerate who has an account.
        raise HTTPException(status_code=401, detail="wrong username or password")

    token = auth.mint_token(_AUTH_SECRET, user["id"])
    # httponly so page scripts can't read it; the browser still sends it
    # automatically on same-origin requests, which is all the client needs.
    # A year-long cookie because the requirement is "log in once" on a
    # phone that then works offline.
    response.set_cookie(
        "auth-token", token, max_age=auth.TOKEN_TTL_S,
        httponly=True, samesite="lax", path="/",
    )
    return {"ok": True, "username": user["name"]}


@app.get("/session")
def session(auth_token: str | None = Cookie(default=None, alias="auth-token")):
    """Lets the client skip the login form when an existing cookie is good."""
    user_id = auth.parse_token(_AUTH_SECRET, auth_token)
    if not user_id:
        return {"authenticated": False}
    conn = auth.connect()
    try:
        user = auth.get_user_by_id(conn, user_id)
    finally:
        conn.close()
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "username": user["name"]}


@app.post("/logout")
def logout(response: Response):
    response.delete_cookie("auth-token", path="/")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Static client.
#
# Mounted last, after every API route, so it can own "/" without
# shadowing them. In production this process serves both the API and the
# built PWA, which is what makes the app same-origin - the cookie above
# is samesite=lax, and the client fetches with credentials, so splitting
# them across origins would break login for no benefit.
#
# html=True makes unknown paths fall back to index.html, which a
# client-side-routed SPA needs.
# ---------------------------------------------------------------------------

# The API lives under /lesphotos-api in dev, because Vite proxies that
# prefix through to this server (client/web/vite.config.ts) so the browser
# sees one origin. Production mounts it at the SAME prefix rather than at
# the root, so the client's URLs are identical in both - the alternative
# was a build-time switch in api.ts, i.e. a code path that only ever runs
# on the NAS and therefore only breaks there.
_CLIENT_DIST = Path(
    os.environ.get(
        "LESPHOTOS_CLIENT_DIST",
        Path(__file__).resolve().parent.parent / "client" / "web" / "dist",
    )
)

root_app = FastAPI()
root_app.mount("/lesphotos-api", app)

if _CLIENT_DIST.is_dir():
    # html=True serves index.html for unknown paths, which the PWA needs.
    # Mounted at "/" and therefore last: it matches everything, so
    # anything mounted after it would be unreachable.
    root_app.mount("/", StaticFiles(directory=_CLIENT_DIST, html=True), name="client")
else:
    log.warning(
        "client build not found at %s - serving API only. Build it with: "
        "cd client/web && npm run build",
        _CLIENT_DIST,
    )
