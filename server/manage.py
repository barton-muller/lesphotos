#!/usr/bin/env python3
"""
User administration, over SSH.

Deliberately a CLI and not an admin UI or an admin role. There are four
users who change roughly never, and every account operation here is one
that should require shell access to the NAS - not a web session that
could be stolen. It also means the running server has no privileged code
path at all: nothing in api.py can create a user, change a password, or
see another library.

    python3 manage.py add-user alice /volume1/homes/alice/Photos
    python3 manage.py list-users
    python3 manage.py passwd alice
    python3 manage.py set-root alice /volume2/photos/alice
    python3 manage.py add-watch-path alice /volume1/homes/alice/Photos/MobileBackup/iPhone
    python3 manage.py remove-watch-path alice /volume1/homes/alice/Photos/MobileBackup/iPhone
    python3 manage.py remove-user alice
"""

import argparse
import getpass
import sys
from pathlib import Path

import auth
import config
from appdb import db


def _prompt_password(user: str) -> str:
    pw = getpass.getpass(f"new password for {user}: ")
    if not pw:
        sys.exit("aborted: empty password")
    if pw != getpass.getpass("repeat: "):
        sys.exit("aborted: passwords did not match")
    return pw


def cmd_add_user(args):
    conn = auth.connect()
    if auth.get_user_by_name(conn, args.name):
        sys.exit(f"user already exists: {args.name}")

    root = Path(args.photos_root).expanduser()
    if not root.is_dir():
        # Refuse rather than create: a typo'd path would otherwise
        # silently make an empty library somewhere unexpected, and the
        # first symptom would be "my photos are all gone".
        sys.exit(f"photos folder does not exist: {root}")

    pw = args.password or _prompt_password(args.name)
    auth.create_user(conn, args.name, pw, root)
    lib = config.Library(root)
    print(f"created {args.name}")
    print(f"  library: {lib.photos_root}")
    print(f"  state:   {lib.sidecar}/  (created on first index)")
    print(f"\nnext: python3 indexer.py --user {args.name} --no-derive")


def cmd_list_users(args):
    conn = auth.connect()
    rows = auth.list_users(conn)
    if not rows:
        print("no users yet - add one with: python3 manage.py add-user <name> <photos-folder>")
        return
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        lib = config.Library(r["photos_root"])
        marker = "" if lib.photos_root.is_dir() else "   [MISSING]"
        # Report the backlog too: it's the number that answers "is this
        # user's library actually usable yet?"
        try:
            conn2 = db.connect(lib)
            total = conn2.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"]
            pending = conn2.execute("SELECT COUNT(*) c FROM media WHERE derived = 0").fetchone()["c"]
            unreadable = conn2.execute("SELECT COUNT(*) c FROM failed_reads").fetchone()["c"]
            conn2.close()
            state = f"{total} photos, {pending} awaiting thumbnails"
            if unreadable:
                state += f", {unreadable} unreadable"
        except Exception:
            state = "not indexed yet"
        print(f"{r['name']:<{width}}  {r['photos_root']}{marker}")
        print(f"{'':<{width}}  {state}")
        watch_paths = auth.get_watch_paths(conn, r["name"])
        if watch_paths:
            print(f"{'':<{width}}  watching: {', '.join(watch_paths)}")


def cmd_passwd(args):
    conn = auth.connect()
    if not auth.get_user_by_name(conn, args.name):
        sys.exit(f"no such user: {args.name}")
    pw = args.password or _prompt_password(args.name)
    auth.set_password(conn, args.name, pw)
    print(f"password updated for {args.name}")
    print("their existing sessions stay valid - tokens are signed with the "
          "server secret, not the password.")


def cmd_set_root(args):
    conn = auth.connect()
    if not auth.get_user_by_name(conn, args.name):
        sys.exit(f"no such user: {args.name}")
    root = Path(args.photos_root).expanduser()
    if not root.is_dir():
        sys.exit(f"photos folder does not exist: {root}")
    auth.set_photos_root(conn, args.name, root)
    print(f"{args.name} -> {root.resolve()}")


def cmd_add_watch_path(args):
    conn = auth.connect()
    if not auth.get_user_by_name(conn, args.name):
        sys.exit(f"no such user: {args.name}")
    path = Path(args.path).expanduser()
    if not path.is_dir():
        sys.exit(f"folder does not exist: {path}")
    auth.add_watch_path(conn, args.name, path)
    print(f"{args.name} now watches: {path.resolve()}")


def cmd_remove_watch_path(args):
    conn = auth.connect()
    if not auth.get_user_by_name(conn, args.name):
        sys.exit(f"no such user: {args.name}")
    auth.remove_watch_path(conn, args.name, Path(args.path).expanduser())
    print(f"{args.name} no longer watches: {args.path}")


def cmd_remove_user(args):
    conn = auth.connect()
    row = auth.get_user_by_name(conn, args.name)
    if not row:
        sys.exit(f"no such user: {args.name}")
    if not args.yes:
        print(f"remove login for {args.name}? Their photos at {row['photos_root']}")
        print("and everything in .lesphotos/ are left untouched.")
        if input("type the username to confirm: ").strip() != args.name:
            sys.exit("aborted")
    auth.delete_user(conn, args.name)
    print(f"removed login for {args.name}. Photos untouched.")


def main():
    ap = argparse.ArgumentParser(
        description="Manage lesphotos users",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-user", help="create an account and point it at a photos folder")
    p.add_argument("name")
    p.add_argument("photos_root")
    p.add_argument("--password", help="skip the prompt (visible in shell history)")
    p.set_defaults(func=cmd_add_user)

    p = sub.add_parser("list-users", help="who exists, where their photos are, indexing state")
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("passwd", help="change a password")
    p.add_argument("name")
    p.add_argument("--password")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("set-root", help="move an account to a different photos folder")
    p.add_argument("name")
    p.add_argument("photos_root")
    p.set_defaults(func=cmd_set_root)

    p = sub.add_parser("add-watch-path", help="watch a folder for new uploads (e.g. a phone backup destination)")
    p.add_argument("name")
    p.add_argument("path")
    p.set_defaults(func=cmd_add_watch_path)

    p = sub.add_parser("remove-watch-path", help="stop watching a folder")
    p.add_argument("name")
    p.add_argument("path")
    p.set_defaults(func=cmd_remove_watch_path)

    p = sub.add_parser("remove-user", help="delete a login (never touches photos)")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_remove_user)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
