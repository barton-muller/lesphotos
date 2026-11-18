# Deploying to the Synology (DS124, DSM, aarch64)

The DS124 has **no Container Manager and no Docker** (Realtek RTD1619B), and DSM's
own Python is 3.8. So this runs natively, with `uv` providing its own Python 3.13 —
DSM's 3.8 is never used and never touched.

Everything below is over SSH.

## 1. Install uv (once)

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv --version
```

If the box has no outbound HTTPS, download the `aarch64-unknown-linux-gnu` tarball
from the uv releases page on the Mac and `scp` it across instead.

## 2. Get the code

Somewhere that survives DSM updates — a normal share, not `/opt`:

```sh
mkdir -p /volume1/lesphotos && cd /volume1/lesphotos
git clone <your-repo-url> .
uv sync          # downloads CPython 3.13 for aarch64, then the deps
```

`uv sync` is also how you apply changes later — see *Making changes*.

## 3. Configure

```sh
cp deploy/lesphotos.env.example deploy/lesphotos.env
python3 -c 'import secrets; print(secrets.token_hex(32))'   # paste as LESPHOTOS_SECRET
mkdir -p /volume1/lesphotos-data
chmod 600 deploy/lesphotos.env
vi deploy/lesphotos.env
```

**`LESPHOTOS_SECRET` must be generated once and kept.** It signs session cookies:
change it and all four users are logged out. The server refuses to start without it,
deliberately — the previous default invented a random one per process, so every NAS
restart silently logged everyone out.

`deploy/lesphotos.env` is gitignored. Don't commit it.

## 4. Build the client

The PWA is static files; the server serves them itself. **Node ≥20.19 is required** —
DSM's own Node package ships v20.9.0, which is too old (Vite 8 / rolldown fail on
`node:util` not exporting `styleText`, added in Node 20.12). With a new enough Node
installed, building on the NAS works and takes about two seconds:

```sh
cd /volume1/lesphotos/app/client/web
npm install
npm run build
```

If the build fails with **"Cannot find native binding … `@rolldown/binding-linux-arm64-gnu`"**,
`node_modules` was installed under a different Node. Delete it and reinstall — keep
`package-lock.json`, which is tracked in git and already lists the arm64 binding:

```sh
rm -rf node_modules && npm install
```

Alternatively build on the Mac and copy the result, which needs nothing installed on the
NAS at all:

```sh
# on the Mac
cd client/web && npm run build
rsync -av dist/ Alice@NAS:/volume1/lesphotos/app/client/web/dist/
```

Either way the server only looks for `dist/` at startup, so **restart it afterwards** or
it keeps serving API-only.

## 5. Create the users

One account per person, each pointed at their own photos folder. There is **no admin
account** — see *Why no admin* below.

```sh
cd /volume1/lesphotos/server
export LESPHOTOS_DATA_DIR=/volume1/lesphotos-data
../.venv/bin/python manage.py add-user alice /volume1/homes/alice/Photos
../.venv/bin/python manage.py add-user carol /volume1/homes/carol/Photos
../.venv/bin/python manage.py list-users
```

Each library keeps its own database and thumbnails in `<library>/.lesphotos/`, so a
user's photos and everything derived from them stay together. Move the folder and its
thumbnails come with it.

## 6. Index — the staged part

**Do not index everything at once.** Two phases, deliberately:

```sh
cd /volume1/lesphotos/server
export LESPHOTOS_DATA_DIR=/volume1/lesphotos-data

# Phase 1: metadata only. No decoding, runs at filesystem speed.
../.venv/bin/python indexer.py --user alice --no-derive

# Phase 2: thumbnails, a batch at a time. Re-run whenever; it resumes.
../.venv/bin/python indexer.py --user alice --derive-pending --limit 200
../.venv/bin/python indexer.py --user alice --status
```

Photos appear in the timeline **as their thumbnails are built** — the timeline only
lists photos that have one, since a tile without a thumbnail would just be a broken
image. So the library fills in progressively rather than appearing all at once.

Run phase 2 as often as you like; `--status` shows what's left.

### Doing the bulk on the Mac

Derivatives are keyed by `photo_id`, so a cache built anywhere is valid anywhere.
Mount the share, index against it, then copy the sidecar over:

```sh
# on the Mac, with the share mounted
cd server
LESPHOTOS_DATA_DIR=/tmp/lesphotos-mac \
  ../.venv/bin/python indexer.py --root "/Volumes/homes/alice/Photos"

# then push the whole sidecar - database and thumbnails together
rsync -av "/Volumes/homes/alice/Photos/.lesphotos/" \
          alice@nas:/volume1/homes/alice/Photos/.lesphotos/
```

Copy `.lesphotos/` as a unit. The database records which photos have thumbnails; move
one without the other and they disagree. Do it while the server is stopped — SQLite
does not like a file changing underneath it. `--root` needs no account, which is why
the Mac doesn't need the auth database.

## 7. Run it

```sh
./deploy/run.sh    # foreground, to check it works
```

Then in DSM: **Control Panel → Task Scheduler → Create → Triggered Task → User-defined
script**, run as your user, event **Boot-up**:

```sh
/volume1/lesphotos/deploy/run.sh >> /volume1/lesphotos-data/server.log 2>&1
```

Open `http://nas.local:8001` (or the NAS IP). DSM's own web UI holds 5000/5001, which
is why this is on 8001.

Reachability off the LAN is a separate decision — see
`docs/decisions/remote-access-quickconnect-vs-tailscale.md`. Don't port-forward this:
it's plain HTTP with a session cookie.

## 8. Optional: the upload watcher

Makes new phone photos show up within moments of upload instead of waiting for someone
to run the indexer by hand. Off by default — no user has any watch path configured
until you set one, and the daemon exits immediately (logging why) if none exist.

```sh
python3 manage.py add-watch-path <user> <folder-the-phone-backs-up-into>
```

Then a **second, separate** Task Scheduler entry, same shape as step 7's but pointing
at `run-watcher.sh` instead of `run.sh` — this is deliberately its own process, not
folded into the server, so a stuck or crashed watcher can never take the API down (see
`server/watcher.py`'s own docstring):

```sh
/volume1/lesphotos/deploy/run-watcher.sh >> /volume1/lesphotos-data/watcher.log 2>&1
```

It moves each new file into `<library>/<YYYY>/<YYYY-MM>/` (not a full dated moment —
that's `auto_generate_moments`' job, run separately/periodically) and indexes just that
one destination folder, so a photo becomes visible without a full library rescan.

## Making changes

> **Partially stale as of 2026-09-25 — mid-migration to a `lesphotos` service
> account.** The API server now genuinely runs as `lesphotos`, **including via its
> own Task Scheduler entry** (updated and verified 2026-09-25 — this now survives a
> reboot). The watcher deliberately stays `root` (see `docs/known-issues.md` for
> why) and its own Task Scheduler entry hasn't been separately re-checked. See
> `docs/known-issues.md`'s "Remaining rollout" entry for exact current status.

Edit, commit, and push on the Mac as usual. Then, over SSH as `alice@nas` (see
CLAUDE.md's "SSH login user" note — `bob@nas` has no account):

```sh
cd /volume1/lesphotos/app
git pull

# The client is built ON THE NAS, not on the Mac + rsync - confirmed working
# directly. node/npm exist at /usr/local/bin but are NOT on a non-interactive SSH
# shell's default PATH (only /usr/bin:/bin:/usr/sbin:/sbin) - an interactive
# `ssh alice@nas` shell finds them fine via DSM's own profile setup, but
# `ssh host 'npm ...'` needs the PATH set explicitly or it fails with
# "npm: command not found" despite npm genuinely being installed.
export PATH=/usr/local/bin:$PATH
cd client/web

# MUST run as lesphotos, not alice - node_modules/dist created by a different
# user blocks lesphotos from executing/writing into them (confirmed: a
# Alice-owned node_modules failed with EACCES on tsc; rebuilding it as
# lesphotos fixed it). If node_modules or dist/ still belong to alice from an
# older build, `sudo rm -rf node_modules dist` once first.
sudo -u lesphotos npm install
sudo -u lesphotos npm run build
cd /volume1/lesphotos/app

# Only if server-side dependencies changed - lesphotos has its OWN independent
# .venv (not shared with alice), so this must also run as lesphotos:
sudo -u lesphotos UV=/usr/local/bin/uv /usr/local/bin/uv sync

# The process is now lesphotos-owned, so killing it needs sudo even as an
# admin user like alice (a plain pkill fails with "Operation not permitted").
sudo pkill -f "uvicorn api:root_app"
```

**Then restart it either way** — both are equivalent and pick up the same code, since
both ultimately run `deploy/run.sh` as `lesphotos`:

- **Via DSM**: Control Panel → Task Scheduler → the API server task → **Run**. This
  is the same path a reboot takes, so it's the better one to use when checking that
  a change will actually survive a restart, not just that it works when hand-run.
- **Via SSH**, for faster iteration without touching the DSM UI:
  ```sh
  sudo -u lesphotos UV=/usr/local/bin/uv /bin/bash deploy/run.sh \
    >> /volume1/lesphotos/data/server.log 2>&1 &
  sleep 2 && curl -I http://localhost:8001    # confirm it came back up
  ```

The restart here is the same as CLAUDE.md's "Restarting the server" section - see
that doc if this drifts from what actually works. **Task Scheduler's own task still
needs updating to match** (both the "Run as" user and the command) before any of
this survives a reboot - not yet done, tracked in `docs/known-issues.md`.

**If a schema change is part of the update** (a new/changed column in
`server/appdb/`), nothing extra is needed to migrate — `db.connect()` runs its
migration on first connection, which happens automatically on the server's next
request. Run `python3 indexer.py --user <name> --no-derive` once per library
afterward only if you want existing rows to pick up a new derived column
immediately rather than waiting for their next natural touch (a move, a favorite
toggle, a re-index) — confirmed safe to run against a live library while the
server is up.

Nothing durable lives in the checkout, so `git pull` can never touch photos, thumbnails,
accounts or stars.

## Why no admin

There is no admin account and no admin UI. Every operation the app has — rename, merge,
star, index — acts on exactly one library, so "acting on your own library" covers all of
them; an admin would exist only to reach across users' photos.

That isolation is structural, not a permission check. An endpoint gets its library from
the session cookie via one dependency, and there is no parameter anywhere that names a
different user's library — so there is no check to forget. Verified: user B asking for
user A's photo by its exact id gets a 404.

Account management is `manage.py` over SSH, which means the running server has no
privileged code path at all — nothing reachable from the web can create a user, change a
password, or read another library.

## Troubleshooting

**Refuses to start, "LESPHOTOS_SECRET is not set"** — intended. Set it in
`deploy/lesphotos.env`.

**"client build not found"** — the server runs API-only. Do step 4.

**Someone is logged out unexpectedly** — `LESPHOTOS_SECRET` changed, or
`deploy/lesphotos.env` isn't being read.

**A photo is missing from the timeline** — it has no thumbnail yet. Check
`indexer.py --user <name> --status`.

**Photos moved or were renamed outside the app** — re-run the indexer with `--prune`
to drop rows whose file is gone. Moving files by hand breaks the invariant that all
moves go through the app (see CLAUDE.md).

**A phone upload never appears, even with the watcher running** — check
`manage.py list-users` shows the watch path under that user, then check the watcher's
own log for the file's name: a permission mismatch between whatever account uploads
the file and whatever account the watcher runs as looks exactly like this and is
silent otherwise (see CLAUDE.md's indexer `failed_reads` notes — the same class of bug,
same fix: `chmod g+r` on the source folder).

## Restarting / stopping

Task Scheduler starts the server at boot but has no stop button, so both restart and
manual stop are done over SSH — see "Restarting the server on the NAS (NAS)" in
CLAUDE.md for the exact commands. In short:

```sh
pkill -f "uvicorn api:root_app"
UV=<absolute path from `which uv`> /bin/bash /volume1/lesphotos/app/deploy/run.sh \
  >> /volume1/lesphotos/data/server.log 2>&1 &
```

The `UV=` override is required in the Task Scheduler command too — DSM's scheduler runs
with no `$HOME` set, so `run.sh`'s own `$HOME/.local/bin/uv` default resolves to
`/.local/bin/uv` and fails.
