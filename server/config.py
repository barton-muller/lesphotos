#!/usr/bin/env python3
"""
Where everything lives.

Previously PHOTOS_ROOT/CACHE_ROOT/DB_PATH were module-level constants in
three separate files, each computed as Path(__file__).parent/"data"/... -
i.e. inside the source checkout. That made the code and the data one
thing: a git pull sat on top of the library, and the app couldn't be
deployed anywhere the source wasn't.

Two rules here, both of which follow from invariant #1 (the filesystem is
the only permanent source of truth):

1. Nothing durable lives in the app directory. A user's derivatives and
   database live *inside their photos folder*, under .lesphotos/. Move
   the library to another disk, another NAS, another app - the thumbnails
   and stars move with it. Nothing to lose track of.

2. The auth database is the one exception: it's about people, not photos,
   so it has nowhere better to be. LESPHOTOS_DATA_DIR, defaulting next to
   the checkout for dev.
"""

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Only holds auth.sqlite3 - see rule 2 above. Per-user photo state does
# NOT live here.
DATA_DIR = Path(os.environ.get("LESPHOTOS_DATA_DIR", _HERE / "data")).resolve()

AUTH_DB_PATH = DATA_DIR / "auth.sqlite3"

# The per-library sidecar directory name. Dotted so iter_media_files'
# existing "skip names starting with ." rule keeps the indexer from
# trying to index its own thumbnails - that recursion would otherwise be
# silent and quadratic.
SIDECAR_DIRNAME = ".lesphotos"


class Library:
    """
    One user's photo library and everything derived from it.

    Deliberately an object rather than module globals. The globals were
    safe only while there was exactly one library; with per-user roots,
    two concurrent requests would race on the same PHOTOS_ROOT that
    _resolve_host_path() validates against - and a path check that reads
    a mutable global is not a path check. Passing this explicitly makes
    "which library is this request touching?" impossible to get wrong.
    """

    def __init__(self, photos_root: Path | str, db_path: Path | str | None = None,
                 cache_root: Path | str | None = None):
        self.photos_root = Path(photos_root).resolve()
        # Overrides, for deriving on a machine that is not the one holding
        # the library: SQLite cannot lock correctly over SMB/NFS, so the
        # database has to be a local copy while the photos stay remote.
        self._db_path = Path(db_path).resolve() if db_path else None
        self._cache_root = Path(cache_root).resolve() if cache_root else None

    @property
    def sidecar(self) -> Path:
        return self.photos_root / SIDECAR_DIRNAME

    @property
    def cache_root(self) -> Path:
        return self._cache_root or self.sidecar / "cache"

    @property
    def db_path(self) -> Path:
        return self._db_path or self.sidecar / "appdb.sqlite3"

    @property
    def stars_json_path(self) -> Path:
        # Plain-text mirror, readable with a text editor in 20 years.
        return self.sidecar / "stars.json"

    def ensure_dirs(self) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"Library({self.photos_root})"
