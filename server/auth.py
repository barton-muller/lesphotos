#!/usr/bin/env python3
"""
Users, passwords and session tokens.

Replaces the single shared LESPHOTOS_PASSWORD. The token now carries a
user id, so every request knows whose library it is addressing - that
identity is what api.py turns into a Library, and it is the only thing
standing between two users' photos.

Password hashing is hashlib.scrypt from the stdlib rather than bcrypt or
argon2: on aarch64 with no Docker, every C-extension dependency is a
wheel that might not exist. scrypt is memory-hard, ships with CPython,
and costs nothing to deploy. Parameters below are the interactive-login
end of the scale - ~64MB per hash, which matters on a 1GB box where four
people log in once and then stay logged in for a month.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config

_SCHEMA_PATH = Path(__file__).resolve().parent / "appdb" / "auth_schema.sql"

# n=2**15 keeps a single hash near ~32MB and well under a second on the
# RTD1619B. Raising n later is safe: the parameters are stored per-hash
# in the encoded string, so old hashes keep verifying.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
# OpenSSL enforces its own scrypt memory ceiling, default 32MB, and n=2**15
# with r=8 needs ~32MB+ - it fails with "memory limit exceeded" rather than
# running slowly, so this has to be raised explicitly. Found by running it,
# not by reading the docs.
_SCRYPT_MAXMEM = 128 * 1024 * 1024

TOKEN_TTL_S = 60 * 60 * 24 * 365  # a year: "log in once", on a phone.


def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_PATH.read_text())
    # CREATE TABLE IF NOT EXISTS above no-ops on an existing `users` table,
    # so watch_paths has to be added by migration for any auth.sqlite3
    # created before this column existed.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "watch_paths" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN watch_paths TEXT")
        conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=32, maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, dk_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
            dklen=len(dk_hex) // 2, maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    # compare_digest, not ==, so a wrong hash can't be recovered byte by
    # byte from response timing.
    return hmac.compare_digest(dk.hex(), dk_hex)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(conn, name: str, password: str, photos_root: str | Path) -> str:
    root = Path(photos_root).expanduser().resolve()
    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, name, pw_hash, photos_root, created_at) VALUES (?,?,?,?,?)",
        (
            user_id,
            name,
            hash_password(password),
            str(root),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return user_id


def set_password(conn, name: str, password: str) -> bool:
    cur = conn.execute(
        "UPDATE users SET pw_hash = ? WHERE name = ?", (hash_password(password), name)
    )
    conn.commit()
    return cur.rowcount > 0


def set_photos_root(conn, name: str, photos_root: str | Path) -> bool:
    root = Path(photos_root).expanduser().resolve()
    cur = conn.execute(
        "UPDATE users SET photos_root = ? WHERE name = ?", (str(root), name)
    )
    conn.commit()
    return cur.rowcount > 0


def get_watch_paths(conn, name: str) -> list[str]:
    row = get_user_by_name(conn, name)
    if row is None or not row["watch_paths"]:
        return []
    return json.loads(row["watch_paths"])


def add_watch_path(conn, name: str, path: str | Path) -> bool:
    resolved = str(Path(path).expanduser().resolve())
    paths = get_watch_paths(conn, name)
    if resolved not in paths:
        paths.append(resolved)
    cur = conn.execute(
        "UPDATE users SET watch_paths = ? WHERE name = ?", (json.dumps(paths), name)
    )
    conn.commit()
    return cur.rowcount > 0


def remove_watch_path(conn, name: str, path: str | Path) -> bool:
    resolved = str(Path(path).expanduser().resolve())
    paths = [p for p in get_watch_paths(conn, name) if p != resolved]
    cur = conn.execute(
        "UPDATE users SET watch_paths = ? WHERE name = ?", (json.dumps(paths), name)
    )
    conn.commit()
    return cur.rowcount > 0


def delete_user(conn, name: str) -> bool:
    """Removes the login only. Photos and their .lesphotos/ sidecar are
    untouched - deleting an account must never delete someone's pictures."""
    cur = conn.execute("DELETE FROM users WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def get_user_by_name(conn, name: str):
    return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()


def get_user_by_id(conn, user_id: str):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY name").fetchall()


def authenticate(conn, name: str, password: str):
    """Returns the user row, or None. Runs a dummy hash on unknown names so
    'no such user' and 'wrong password' take the same time."""
    row = get_user_by_name(conn, name)
    if row is None:
        hash_password(password)
        return None
    return row if verify_password(password, row["pw_hash"]) else None


def library_for(user_row) -> config.Library:
    return config.Library(user_row["photos_root"])


# ---------------------------------------------------------------------------
# Session tokens
#
# Stateless HMAC over (user_id, expiry) - the server holds no session
# table, and a stolen token expires on its own. The signing secret must
# be pinned via LESPHOTOS_SECRET or every restart logs all four people
# out, which on a NAS that reboots is the difference between "log in
# once" and "log in constantly".
# ---------------------------------------------------------------------------

class SecretMissing(RuntimeError):
    pass


def load_secret(required: bool = True) -> str:
    secret = os.environ.get("LESPHOTOS_SECRET")
    if secret:
        return secret
    if required:
        raise SecretMissing(
            "LESPHOTOS_SECRET is not set. Without it a fresh random secret is "
            "generated per process, so every restart invalidates all sessions. "
            "Generate one with:  python3 -c 'import secrets;print(secrets.token_hex(32))'"
        )
    return secrets.token_hex(32)


def mint_token(secret: str, user_id: str, ttl_s: int = TOKEN_TTL_S) -> str:
    expiry = str(int(time.time()) + ttl_s)
    payload = f"{user_id}.{expiry}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def parse_token(secret: str, token: str | None) -> str | None:
    """Returns the user_id if the token is well-formed, correctly signed and
    unexpired; otherwise None."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    user_id, expiry, sig = parts
    expected = hmac.new(
        secret.encode(), f"{user_id}.{expiry}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        if int(expiry) <= time.time():
            return None
    except ValueError:
        return None
    return user_id
