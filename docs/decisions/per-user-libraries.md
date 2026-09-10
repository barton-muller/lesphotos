# Per-user libraries, and no admin

## The problem

The app had one shared household password and one library. Four people needed their
own photos, each in their own folder, with no one able to see anyone else's.

## What was decided

**One SQLite database per user, living inside that user's photos folder** — not one
database with a `user_id` column, and not a per-user database in an app directory.

```
/volume1/homes/alice/Photos/
    2026-03-15-Skiing/...
    .lesphotos/
        appdb.sqlite3      <- this library's database
        cache/<photo_id>/  <- its thumbnails
        stars.json         <- plain-text mirror
```

**There is no admin account and no admin role.**

## Why

### Separate databases, not a user_id column

A `user_id` column makes isolation a property of every query: one missing `WHERE`
clause leaks another person's photos, and nothing fails loudly when it happens. With
separate files the query *cannot* see another user's rows, because it is not connected
to that database. The isolation is structural rather than a rule to be remembered.

The cost is that a genuinely shared library would now be real work. That was accepted:
nobody asked for one, and the requirement that was actually stated ("if possible no
admin with access to other people's photos") points the other way.

### Inside the photos folder, not an app directory

Follows invariant #1 — the filesystem is the only permanent source of truth. Photos and
everything derived from them travel together: move the folder to another volume, another
NAS, or another application, and the thumbnails, moment labels and stars come with it.
State in `/volume1/lesphotos-data/alice/` would be one reorganisation away from being
orphaned, and the orphaning would be silent.

It also makes the Mac→NAS bulk-thumbnail workflow an ordinary folder copy: derivatives
are keyed by `photo_id`, so a cache built anywhere is valid anywhere.

The sidecar is `.lesphotos/`, dot-prefixed, because `iter_media_files` already skips
dot-directories — without that the indexer would recurse into its own thumbnail output.

Auth is the one exception: `auth.sqlite3` is about people, not photos, so it has nowhere
better to live and sits in `LESPHOTOS_DATA_DIR`.

### No admin

Every mutation the app has — rename, merge, star, index — acts on exactly one library.
"Acting on your own library" therefore covers all of them, and an admin would exist only
to reach across users' photos, which is the one thing this design is meant to prevent.

Account management is `manage.py` over SSH instead. The running server has no privileged
code path at all: nothing reachable from the web can create a user, change a password, or
read another library. A stolen session cookie gets one library and nothing else.

## What this changed in the code

`PHOTOS_ROOT`, `CACHE_ROOT` and `DB_PATH` were module-level constants computed from
`__file__` — i.e. inside the source checkout. They are now a `config.Library` object
passed explicitly (`db.connect(library)`, `indexer.index(library)`,
`reorganize.rename_moment(library, ...)`).

Explicit passing matters for more than tidiness: `_resolve_host_path()` validates
client-supplied paths against the library root (invariant #5). Reading a mutable module
global there was safe only while exactly one library existed — with per-user roots, two
concurrent requests would race on it, and a path check that reads a shared global is not
a path check.

In `api.py` a single `current` dependency turns the session cookie into a `Session`
carrying the user and their `Library`. That is the only way an endpoint obtains a
library, so there is no parameter anywhere naming someone else's — no check to forget.

Two bugs surfaced while doing this:

- `/media/{id}/thumb` and `/preview` took **no authentication at all**. Survivable with
  one shared password; a cross-user leak with four. Both now require a session and read
  from that session's cache root.
- Derivatives were served `Cache-Control: public`. With per-user libraries these are one
  person's photos and must never sit in a shared proxy cache — now `private`.

## Sessions

The token is an HMAC over `(user_id, expiry)`, so the server holds no session state.
`LESPHOTOS_SECRET` is now **required**: it previously defaulted to a fresh random value
per process, which meant every restart logged everyone out. On a NAS that reboots, that
silently breaks the "log in once" requirement, so the server now refuses to start rather
than appearing to work. TTL is a year, because the phones are meant to log in once and
then work offline.

Passwords use `hashlib.scrypt` from the stdlib rather than bcrypt or argon2: on aarch64
with no Docker, every C-extension dependency is a wheel that might not exist. Note that
OpenSSL enforces its own scrypt memory ceiling (default 32MB) and `n=2**15, r=8` exceeds
it — `maxmem` must be passed explicitly or hashing fails outright with "memory limit
exceeded".

## Verified

Two users, two libraries, driven with curl against a live server from a cold start:
each sees only their own timeline; user B requesting user A's photo *by its exact
`photo_id`* gets 404 for thumb, preview, original and metadata; path traversal
(`../alice/...`, `/etc/passwd`) is rejected with 400; sessions survive a server restart.
