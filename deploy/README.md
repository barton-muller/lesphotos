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
../.venv/bin/python manage.py add-user bob   /volume1/homes/bob/Photos
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

## Making changes

```sh
# on the Mac: edit, commit, push, and rebuild the client if it changed
cd client/web && npm run build
rsync -av dist/ alice@nas:/volume1/lesphotos/client/web/dist/

# on the NAS
cd /volume1/lesphotos
git pull
uv sync                    # only if dependencies changed
# Task Scheduler -> select the task -> Stop, then Run
```

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

## Outstanding

- **Task Scheduler boot task** (step 7) is not set up yet. The server currently runs via
  `nohup ./deploy/run.sh &`, which survives an SSH session closing but **not a reboot**.
  `tailscale serve --bg` IS persistent across reboots, so after a restart the HTTPS front
  end will return 502 with nothing behind it until the app is started by hand. Do this
  before other people depend on the app.
