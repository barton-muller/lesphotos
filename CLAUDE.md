# CLAUDE.md

Orientation for anyone (human or Claude) picking up this repo. `README.md` has the
architecture, design rationale and commands — read it first. This file is the part that isn't
obvious from the code: invariants that must not be broken, and bugs that were expensive to
find once.

Target hardware is a **Synology DS124: 1GB RAM, ARM, single 3.5" SATA bay holding a real
spinning HDD** (confirmed via SSH: `rotational=1`, a Toshiba HAT3300 — not an SSD, and this
model has no room for a second internal drive; only a USB 3.2 Gen 1 port for external
storage). Nearly every choice here follows from that. Don't assume something scales without
measuring it on real data — this project has a consistent history of otherwise-correct-looking
code failing at real library size. The disk being genuinely rotational specifically explains
why serving many small scattered files (e.g. a bulk thumbnail download) needs different
handling than it would on any dev machine's SSD — see `docs/decisions/thumb-shards.md`.

## Hard invariants

1. **The filesystem is the only permanent source of truth.** Moments are directories named
   `YYYY-MM-DD-Label`. A library's `appdb.sqlite3` and `cache/` are both rebuildable by
   re-running the indexer. The one exception is stars, which have no other source — hence
   the plain-text `stars.json` mirror.

   **All of it lives inside the library it describes**, at `<photos-root>/.lesphotos/`
   — never in an app directory. Move the folder and the thumbnails, labels and stars go
   with it. `auth.sqlite3` (accounts) is the sole exception: it is about people, not
   photos. See `docs/decisions/per-user-libraries.md`.

2. **Originals are never modified.** No EXIF/XMP writes to store app state. Test photos used
   by the fakelib generators are copied, never edited in place.

3. **All moves/renames go through the app**, never by hand in Finder. This is what makes
   appdb safe to treat as authoritative rather than needing to reconcile against surprise
   external changes. `reorganize.py` is the only thing that moves files.

4. **`photo_id` is permanent.** Minted once per photo, kept across every move. Derivatives
   live at `cache/<photo_id>/`, so a rename doesn't invalidate them. Never key anything
   durable on a path or a content hash — a path changes on every move, and a hash changes if
   a photo is ever edited in place, which would orphan its stars.

5. **Never accept a client-supplied path without validating it stays under the caller's
   own library root.** `_resolve_host_path()` in `api.py`; `photo_id` params are
   regex-validated as bare UUIDs.

6. **A request's library comes from its session, never from a parameter or a global.**
   `Depends(current)` yields a `Session` carrying the user's `config.Library`; everything
   downstream takes paths and databases from it. This is what makes cross-user access
   impossible rather than merely forbidden — there is no way to *name* another user's
   library, so there is no check to forget. Never reintroduce a module-level
   `PHOTOS_ROOT`: two concurrent requests would race on it, and a path check that reads a
   mutable global is not a path check.

7. **No admin, anywhere.** Every mutation acts on one library, so "your own library"
   covers all of them. Account management is `manage.py` over SSH, so the running server
   has no privileged code path at all. Don't add an admin role or an admin endpoint.

8. **The client's IndexedDB `favorite` field is `0 | 1`, never `boolean`.** IndexedDB's key
   types don't include boolean, so a boolean-typed indexed field silently can't be queried by
   `.equals()`. Any new indexed boolean-ish field must use `0 | 1` from the start.

## Gotchas worth not rediscovering

### Indexing / images

- **`Image.draft()` only downscales in powers of two, and only when the target is at least
  half the source.** `PREVIEW_MAX=1600` against a 3072px photo silently did *not* scale and
  decoded full-size (193MB). That's why it's **1400** — don't "tidy" it upward.
- **Shrink before transposing/converting.** Each PIL op allocates a buffer sized to what it
  operates on, so resizing last makes every intermediate full-size. Reordering: 162MB → 110MB.
- **HEIC decodes in a subprocess** (`_derive_heic_isolated`). pillow-heif has no scaled-decode
  path and `draft()` is a no-op for HEIC, so one 24MP iPhone HEIC costs ~250MB in-process.
  ffmpeg was tried and **rejected**: iPhone HEICs are grids of 512×512 tiles that ffmpeg
  exposes as separate streams, so `-map 0:v:0` silently yields one square tile. Caught only by
  checking output dimensions.
- **Don't assume an ffmpeg build has a webp encoder** — the dev machine's doesn't. ffmpeg
  writes JPEG; Pillow re-encodes.
- **Apple MakerNote offsets are relative to the START of the MakerNote block**, not the
  TIFF header inside it, and Apple truncates that header (`MM` + 2-byte version, IFD
  immediately after — not the standard 8 bytes). Get either wrong and parsing fails
  *silently*: the wrong base returns a truncated but still UUID-shaped string, and the
  wrong header offset gives an entry count of 0. Both were found by testing against a real
  file, not by reading a spec.
- **A `.MOV`'s ContentIdentifier can be megabytes into the file** (1.2MB into a 4MB clip on
  a real iPhone), so `quicktime_content_id` reads the whole file and is called only for
  siblings of a still — never for every file.
- **The incremental skip path never reaches per-file detection.** Anything derived from
  file *contents* that is added later must also be backfilled in a pass that visits every
  row, or existing libraries stay blank forever — this silently missed 1,565 Motion Photos
  until `link_motion_pairs` learned to fill them in.
- **Host-specific workarounds live in `server/platform_hosts.py`, not in the indexer.**
  Core code asks a generic question (`existing_video_poster`, `IGNORED_DIRNAMES`) and that
  file holds the host-shaped answer, so porting to another NAS means editing one file.
  Detection is by looking for the artifact, never by sniffing the platform — a library
  rsynced off a Synology keeps its posters and should still use them.
- **Synology Photos indexes `.lesphotos/cache/` even though it's a dotfile.** Dot-hiding
  (which is what keeps our own indexer from recursing into it, see `SIDECAR_DIRNAME` in
  `config.py`) is a Unix convention that Synology's Photos app does not honor — it walked
  the folder anyway and showed every thumbnail as a "photo" to end users. There is no
  per-folder exclusion in the Synology Photos UI, and `synoindex -D <path>` (the fix for
  the older PhotoStation package) had no effect — the Photos app apparently uses a
  different indexing path. What worked: **Synology Photos → Settings → excluded file
  extensions → add `webp`.** Cache derivatives are always exactly `thumb.webp` /
  `preview.webp` (`_derive_one` in `indexer.py`), so this is a clean, low-collateral filter
  — the only cost is that a library can no longer contain a real `.webp` *original* photo
  without it also being hidden from Synology Photos, which in practice is a non-issue.
- **Synology's ffmpeg has NO video decoders** — no hevc, no h264, and no `/dev/dri`
  (DS124, ffmpeg 4.1.9). Extracting a frame on the NAS is impossible, so video thumbnails
  come from DSM Image Assistant's own poster at
  `<dir>/@eaDir/<filename>/SYNOPHOTO_THUMB_XL.jpg` (1280px, already rotation-corrected;
  100% coverage on the real library). Only the pixels are borrowed, once, at derive time —
  nothing is ever served from `@eaDir`, so its path-keyed coupling doesn't outlive
  `derive_video_thumb`. ffmpeg extraction stays as the fallback for dev machines.
- **That build also lacks the `image2` muxer** while having the `mjpeg` encoder, so
  writing `out.jpg` fails with the misleading "Unable to find a suitable output format"
  *before* encoding. `_ffmpeg_scaled` passes `-f mjpeg` explicitly.
- **Video metadata comes from `ffmpeg`, never `ffprobe`.** DSM ships `/bin/ffmpeg` with no
  `ffprobe` at all (DS124, ffmpeg 4.1.9) and there's no package manager to add one, so
  `probe_video` parses `ffmpeg -i` stderr. Don't "improve" it back to ffprobe — every
  video would silently fail to derive and vanish from the timeline, which only lists
  derived rows.
- **Grouping reads `taken_at` from appdb, not from the file.** `read_capture_datetime` only
  understands EXIF, so it returns file *mtime* for every video — the date it was copied.
  The indexer already stores the real container date (via ffmpeg), so `auto_generate_moments`
  reads that back and only falls back to opening the file for paths it has no row for. This
  is also the difference between a 3ms plan and minutes of header reads on the NAS.
- **Any module that reads dates or pixels from a `.heic` must call `register_heif_opener()`.**
  `reorganize.py` did not, so `read_capture_datetime` could not open a HEIC at all and fell
  through to file mtime — filing every iPhone photo under the date it was *copied*. The
  fallback always succeeds, so the failure is completely invisible; it only shows up as
  photos landing in the wrong moment.
- **`geocoding.read_gps` returns a `(None, None)` tuple, not `None`,** for a photo with no GPS.
  It's truthy. Check the values.
- **`Image.open()` reads the header without decoding**, which is what makes
  `--no-derive` run at filesystem speed. The expensive part of indexing is always the
  decode, never the metadata.
- **`/timeline` filters on `derived = 1`**, so a photo is invisible until it has a
  thumbnail. That is deliberate (a tile with no thumbnail is a broken image) and it is
  why a staged index makes the library fill in progressively. Don't "fix" it.

### Client

- **A `useEffect` dep list holding an object re-fires on every parent re-render.**
  `PhotoViewer` used `[photo]` while `Timeline` builds fresh objects each render, so the
  in-flight full-res fetch was cancelled repeatedly and the viewer showed the thumbnail
  forever. Key on `photo.id`. Related: don't revoke an object URL in the same effect's
  cleanup — revoke it when its replacement is ready.
- **`/timeline/count` must use the exact same WHERE as `/timeline`** (`derived = 1 AND
  is_motion_clip = 0`). The client trusts a count match to skip a full relist entirely, so a
  mismatched filter would either force needless relists or, worse, wrongly skip one.
- **Metadata sync must write incrementally, per server page, never buffer the whole
  library then write once.** Buffering meant the status bar and the grid both stayed blank
  for the full sync duration (30-60s+ on a few thousand photos), and thumbnails for early
  years never got a chance to start loading until the write pass reached them - visible as
  bands of grey tiles unrelated to what was on screen. `syncTimeline` writes each page as
  it lands; `downloadAllThumbnails` is thumbnail PIXELS as a separate, explicit action.
- **A bulk operation over IndexedDB rows should be resumable by re-querying "what's still
  missing", not by tracking progress state.** `downloadAllThumbnails` re-derives its work
  list (`rows without a blob`) on every call, so a tab closed or reloaded mid-download loses
  nothing - the next run picks up exactly where it stopped. Verified: interrupted at 1,953
  of 2,298, re-invoked, completed to 2,298 with no duplication.
- **Prefetch must never hit the network.** It fires on every scroll position change over a
  runway of ~72 tiles either side — right for cheap IndexedDB reads, catastrophic once
  `loadThumbnailUrl` could fetch: a fling requested all 2,298 photos of a 2,298-photo
  library. `prefetchThumbnails` passes `allowNetwork: false`; only a mounted tile fetches.
- **`useThumbnail`'s retain/release must key on `id` alone.** Keyed on `[id, resident]` it
  releases and re-retains every time the URL resolves, so the count hits zero while the
  tile is still mounted and aborts the fetch it is waiting for — the symptom was one tile
  rendering out of thirty.
- **`thumbnailBlob` is optional.** A sync stores metadata only by default and the grid
  fetches each thumbnail as its tile scrolls into view, so a new device is usable at once
  instead of after a full-library download. "Download all for offline" is the explicit
  opt-in that makes the whole library offline-capable. Anything reading a row must handle
  a missing blob.
- **A stable primary key means "already cached" no longer means "unchanged".** `sync.ts` must
  diff metadata, not just key presence: a rename keeps the same `photo_id` by design, so an
  id-presence check alone silently skips every moved photo forever.
- **A plain `fetch()` GET can be served from the browser's HTTP cache with no console error.**
  `/timeline` sets `Cache-Control: no-store` server-side *and* `cache: 'no-store'`
  client-side. Without both, a sync can silently read pre-move data — this caused "created a
  moment, synced fine, still not visible".
- **Dexie version bumps don't fix existing row *values*, only add the index.** If a stored
  type needs to change, the `.upgrade()` callback must actively rewrite rows.
- **`vite-plugin-pwa`'s service worker is disabled under `vite dev`.** Test offline behaviour
  against `npm run build && npm run preview`.
- **A service worker also needs a secure context** — HTTPS or `localhost`. Browsing the NAS
  over plain HTTP (`http://nas.local:8001`, a LAN IP, or a Tailscale hostname) registers
  nothing, with no error: the app works but has no offline capability, and it simply does
  not appear under Safari's Develop → Service Workers. `sw.js` being served correctly is
  not evidence it registered; check `window.isSecureContext`.
- **A `useEffect(() => {...}, [])` reading a `useRef` only fires at initial mount** — if the
  node is behind a loading early-return it's still null, and never gets a second chance. Use a
  callback ref.
- **`<a download>` elements must be appended to the DOM before `.click()`**, or it silently
  does nothing. And don't swallow `AbortError` as success when a permission prompt is
  cancelled — fall through to the next fallback tier.

### Server

- **`sqlite3.Connection` has no `__dict__`** — you cannot attach an attribute to a
  connection. `db.connect()` returns a small subclass so the `Library` can travel with it.
- **OpenSSL caps scrypt memory at 32MB by default**, and `n=2**15, r=8` exceeds it:
  hashing fails outright with "memory limit exceeded" unless `maxmem` is passed
  explicitly. Fails loudly, but only at runtime.
- **`LESPHOTOS_SECRET` is required and must be stable.** It signs session cookies; a
  changed or absent value logs all users out. It used to default to a per-process random
  value, which meant every NAS restart silently logged everyone out — the server now
  refuses to start instead.
- **The API is mounted under `/lesphotos-api` in production too**, not at the root. Vite
  proxies that prefix in dev, so keeping it in production means the client's URLs are
  identical in both — the alternative was a build-time switch that only ever executes on
  the NAS, and therefore only breaks there.
- **The static client mount must stay last.** It is mounted at `/` with `html=True` and
  matches everything, so anything mounted after it is unreachable.

- **`reorganize.py` validates every path before touching the filesystem.** The caller's list
  can legitimately drift from disk. `api.py`'s `_run_reorganize()` turns `FileNotFoundError`/
  `FileExistsError` into a clean **409**, not a 500 — route any new reorganize-calling
  endpoint through it.
- **Everything runs synchronously in the request.** No job queue. Fine at 54 photos in 296ms;
  a bulk `auto-generate` across thousands would tie up a worker.

## Testing

### Starting the app locally

The app **cannot start without `LESPHOTOS_SECRET`**, and **cannot be logged into until a
user exists** — both deliberate (see `docs/decisions/per-user-libraries.md`). If
`manage.py list-users` prints nothing, the answer is to create a user, not to work around
the login.

```sh
cd server
export LESPHOTOS_DATA_DIR="$PWD/data"          # where auth.sqlite3 lives
../.venv/bin/python manage.py list-users        # who already exists

# if empty, create one. The library root is the USER's folder, not the
# parent: data/photos/bob, never data/photos.
../.venv/bin/python manage.py add-user <name> "$PWD/data/photos/bob"

cd .. && UV=$(command -v uv) ./deploy/run.sh    # reads deploy/lesphotos.env
```

`deploy/lesphotos.env` is gitignored and holds the secret; `run.sh` fails loudly if it is
missing. Serving is `api:root_app` (client + API), not `api:app` (API alone) — the client
calls `/lesphotos-api/*`, so `api:app` alone gives 404s on every request.

**Don't invent a new secret to get unblocked.** A fresh one invalidates every existing
session, which looks like "login is broken" to anyone else using the machine.

### Dev server ports

**Local dev always uses backend `:8001` and Vite `:5173` — the defaults, not a spare
port.** These are Claude's own instances, not anyone else's: the user keeps browser tabs
open on them across sessions and expects `git pull` / a restart to be enough to see new
work, not a fresh port to hunt for each time. Reuse them:

- If something is already listening on 8001/5173, it is almost certainly a stale instance
  from an earlier session — kill it and restart on the same port rather than picking a
  different one.
- Don't invent `LESPHOTOS_PORT=<random>` "to be safe". The one case for a second port is
  running two working trees side by side on purpose (e.g. comparing a stash against
  current) — pick an obvious throwaway port for that, and stop it before finishing up
  rather than leaving it bound.

### Restarting the server on the NAS (NAS)

The server is started at boot by a DSM **Task Scheduler** triggered task (Control Panel →
Task Scheduler, event Boot-up), but Task Scheduler has no "kill this task" button — the
UI can re-run the start script but cannot stop a process it started. So in practice,
restarting is done entirely from an SSH session, never by logging into the DSM web UI.

**SSH as `alice@nas`** — not `bob@nas`, which has no account on the NAS and just
hangs on password auth with no way to fail cleanly.

```sh
ssh alice@nas

pkill -f "uvicorn api:root_app"

UV=/var/services/homes/Alice/.local/bin/uv /bin/bash /volume1/lesphotos/app/deploy/run.sh \
  >> /volume1/lesphotos/data/server.log 2>&1 &

sleep 2 && curl -I http://localhost:8001    # confirm it came back up
```

Two things that aren't obvious from `deploy/run.sh` alone:

- **`run.sh` looks for `uv` at `$HOME/.local/bin/uv`, but DSM Task Scheduler runs with no
  `$HOME` set** (it's not a login shell) — the lookup silently becomes `/.local/bin/uv`
  and fails with "uv not found." The fix is passing `UV=<absolute path to uv>` explicitly,
  which is why it's in the command above rather than relying on `run.sh`'s own default.
  Find the real path once with `which uv` over SSH (it's `/var/services/homes/<user>/...`,
  not `/volume1/homes/<user>/...` — DSM's actual home path differs from the share path).
- **The Task Scheduler "Run command" must match this exact command** (with `/bin/bash`
  in front — Synology's own recommendation for scheduler scripts — and the same `UV=`
  prefix), or the boot-time start will fail the same way. If the paths above ever change
  (repo moved, different NAS user), update the Task Scheduler entry to match, not just
  this doc.

The repo's checkout on this NAS is at `/volume1/lesphotos/app` (the clone is named `app`,
not `lesphotos`), with `LESPHOTOS_DATA_DIR=/volume1/lesphotos/data` as a sibling — adjust
the paths above if that ever changes. See `deploy/README.md` for full install/deploy
steps; this section only covers restart.

### How validation is done

There's no unit suite. Validation is **driving the real running app** — Playwright against the
production build, and `curl` against a live server. This has repeatedly caught bugs that
reading the code missed: the cookie auth model, service-worker dev behaviour, pagination
truncation, an IndexedDB migration gap, and a full-res race that only appeared once the
backend got fast enough to lose it.

Two lessons that keep recurring:

- **Density/visual changes need a real screenshot at real data scale.** Label-collision and
  contrast bugs looked fine in code and were plainly broken on screen.
- **Test from a cold start, not from warm process state.** A rename once "passed" only because
  an auth token was cached in memory from earlier in the session; it failed for anyone
  starting the server fresh.
- **Test multi-user changes with two real users and two real libraries.** The isolation
  claim is only worth anything if it has been checked from the other side: log in as B and
  request A's photo *by its exact `photo_id`*. That is what caught `/media/{id}/thumb`
  having no authentication at all.
- **Generate distinct test images.** `photos` is keyed by `content_hash`, so identical
  solid-colour JPEGs collapse into a single row and every count silently reads wrong.

Scratch test scripts are written ad hoc and deleted after — don't commit them unless one is
worth maintaining as a regression check.

## Where decisions live

- `docs/decisions/` — one file per real design choice, with the options considered and why.
- `docs/archive/plan-photoview-era.md` — the running log from the PhotoView era. Kept for
  archaeology only; **it describes a backend that no longer exists**.
- Commit at real milestones, not every save. Never push without being asked.
