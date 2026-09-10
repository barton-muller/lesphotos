# ARCHIVE — PhotoView-era running log (superseded)

> This is the development log from when the backend was **PhotoView**, kept only for
> archaeology. PhotoView was replaced by a first-party indexer + FastAPI serving layer in
> August 2026 and is no longer part of the system — see
> [replacing-photoview.md](../decisions/replacing-photoview.md) for what changed and why,
> and the repo `README.md` for how things actually work now.
>
> **Do not treat anything below as current.** Much of it describes APIs, constraints and
> workarounds that no longer exist.

---

# Self-hosted Photo App — Prototype: Fake Library + PhotoView + Sync Client

## Context

The long-term goal (see the design doc discussed alongside this plan) is a filesystem-first,
self-hosted photo system running on a 1GB-RAM Synology DS124, browsed instantly via a
locally-cached thumbnail PWA on phones/laptops, with the filesystem as the only source of
truth. Before touching real hardware or a real photo library, the plan calls for **Phase 1:
test whether PhotoView is actually a viable backend** (RAM/CPU headroom on ARM, timeline
behavior, multi-user, thumbnailing) rather than building a custom backend from scratch.

This repo currently contains only `initial_script.py`, written by the user's dad: a DSM
in-place sorter that walks a `PhotoDump`, reads EXIF date + GPS, reverse-geocodes, groups
photos into events by a 2-day time gap (always force-splitting at year boundaries), and moves
each event into `<root>/YYYY/YYYY-MM-DD-city/`, dragging Synology's `@eaDir` thumbnail cache
folders along, with a dry-run mode and an undo log.

Through discussion, several decisions were made about how the prototype differs from both the
original design doc and dad's script:

- **Folder scheme**: `<root>/YYYY/YYYY-MM-DD-label/` — one level under the year, using dad's
  gap-based event detection (2-day default gap). Unlike dad's script, **no forced split at
  year boundaries** — a moment that spills from Dec into Jan stays in one folder, filed under
  the year of its *first* photo.
- **No geocoding** — dad's script's Nominatim reverse-geocoding is dropped entirely for now.
  Folder labels are simple (`YYYY-MM-DD-moment`); the intent is for the PWA to eventually let
  the user rename moments by hand (e.g. `2024-03-15-LeedsWithDad`), possibly with an
  auto-geocoded suggestion as a later nicety, not a hard requirement.
- **Backend**: test real PhotoView (Docker, official image) against a fake photo library
  first, rather than committing to a custom backend. Build-vs-buy experiment — if PhotoView's
  RAM/CPU footprint and API model hold up on the constrained hardware, it becomes the
  foundation; if not, drop out and build custom.
- **Sync model**: PhotoView has no delta/changes-since-version API. Confirmed against the real
  server: `myTimeline(fromDate: ...)` is **not** a "changes since" filter — it's a
  chronological pagination cursor that returns items with EXIF **capture date ≤ fromDate**
  (i.e. "give me the timeline page ending at this point"), so it cannot be used to ask for
  "photos added since my last sync." There is no server-side "date added"/"date scanned"
  field exposed on `Media` either. Given that, the client instead does a **full `myAlbums`
  re-list every sync**, diffing the returned media IDs against its own locally-cached ID set
  to find additions/removals — proven to work in this prototype (62 media items, ~cheap at
  this scale). This is less elegant than a true delta API, but it's what PhotoView's actual
  schema supports; a real client should still avoid re-downloading thumbnail bytes for
  already-cached IDs (which the prototype already does), even though the listing itself is
  always full.
- **Real-world upload path matters for this test.** Actual capture-to-NAS upload will go
  through the **Synology Photos mobile app** (auto-backup from the phone), not through
  PhotoView or this sorter. Synology Photos writes into its own default `Photos/YYYY/MM/`
  layout and generates its own thumbnails client-side (phone does the HEIC decode). This
  changes what the sorter needs to do — see "Synology Photos interaction" below — and means
  PhotoView's own thumbnailing/scanning of raw dumped files is a nice-to-have fallback, not
  the primary path.
- **Originals-on-device shortcut**: when a photo's original is already on the device that
  captured it (i.e. the client's own camera roll / local cache), the app should open that
  local file directly rather than re-fetching the "original" from the server. The server round
  trip for originals is only for photos not already local to that device.
- **Scope of this pass**: run real PhotoView in Docker against a generated fake `/Photos`
  tree, validate scanning/timeline/favorites/multi-user + watch resource usage, and build a
  minimal script that exercises the GraphQL API to prove the "local thumbnail sync" idea
  end-to-end. All local — no real NAS, no real photo library yet.

## Synology Photos interaction — a real risk, not just theoretical

Researched directly because it affects whether dad's script's raw `shutil.move` approach is
safe to ever run against a live Synology-Photos-backed library:

- Synology Photos' mobile backup matches "already uploaded" photos against the NAS **by
  path/presence**, not a durable content hash independent of location. Synology's own docs
  describe a "Scan and back up all photos" mode that **re-triggers upload** (documented
  duplicate-backup behavior) if a previously-uploaded file was renamed, moved, or deleted
  server-side.
- DSM's underlying `synoindex` media database is **path-keyed**. A raw `mv`/rename outside
  Synology Photos leaves stale index entries, and later re-indexing can duplicate the DB
  entry rather than update it in place.
- The **sanctioned safe move** is `synoindex -N newfullpath oldfullpath` (what File Station's
  own move/rename calls internally), not a raw filesystem move — this is exactly the gap in
  dad's script, which does `shutil.move` directly.
- Conclusion for later phases (not required for this prototype pass, since we're not touching
  Synology Photos yet): **any real sorter that reorganizes a Synology-Photos-managed library
  must call `synoindex -N` per file after each move**, and should avoid triggering "scan and
  back up all photos" in the app afterward. This is a documented, known failure mode — not
  speculation — so it needs to be handled before dad's script (or its descendant) is ever run
  against the real live-uploaded library. Flagged here for Phase 3+ of the real plan; this
  prototype pass doesn't touch Synology Photos at all, so it's out of scope for now but must
  not be forgotten.

**Confirmed directly against NAS via SSH (bypassing SMB's veto-files filtering, which hides
`@eaDir` from any SMB client regardless of whether it actually exists — a real gap in earlier
SMB-only checks, corrected here):**

- `ls @eaDir/` on a real Synology-Photos-uploaded folder (`Photos/MobileBackup/iPhone/2026/08/`)
  shows only `<filename>` and `<filename>@SynoEAStream` entries — **no `SYNOPHOTO_THUMB_*` or
  `SYNOFILE_THUMB_*` files**. `@SynoEAStream` is Synology's extended-attribute stream storage
  (metadata, not an image cache). **Dad's script's core assumption — that moving a file means
  dragging along an `@eaDir/<filename>/SYNOFILE_THUMB_*` thumbnail folder — does not apply to
  modern Synology-Photos-managed uploads.**
- `sudo find /volume1 -maxdepth 3 -iname "*photo*"` located
  **`/volume1/@appdata/SynologyPhotos`** — the modern Synology Photos package's own private,
  centralized data directory (thumbnails + its `synofoto` PostgreSQL database, per earlier
  research), entirely separate from `@eaDir` and from the original photo files under
  `homes/*/Photos`. This is a *per-package* cache, not a *per-file* sidecar.
- The same `find` also surfaced `/volume1/@eaDir/photoalbums` and `/volume1/photoalbums/...`
  (with folder names like `06-06-07 Nl visit & Pas garden photos`) — this is a **separate,
  older/legacy structure** (dad's original "PhotoAlbums" setup, likely from DSM's legacy Photo
  Station), distinct from the newer `homes/*/Photos` + Synology Photos app path. `@eaDir`
  thumbnails may genuinely exist and matter *there*, but that's not the tree this project's
  fake library or any future real ingest is modeled on.
- **Practical implication**: a real mover reorganizing `homes/*/Photos` needs **no
  thumbnail-dragging logic at all** for Synology-Photos-uploaded content — Synology Photos'
  own cache isn't file-adjacent. The real, still-open problem for that tree is exactly the
  `synofoto` database desync already described above (raw `mv` breaks Synology Photos' own
  path-keyed tracking of "this photo I already indexed/uploaded"), which is a metadata-sync
  problem, not a lost-thumbnail problem.

## Repo layout — server vs. client made explicit

```
lesphotos/
├── plan.md                     # this file
├── initial_script.py           # dad's script — kept as-is for reference
├── pyproject.toml              # uv-managed, single project for the Python tooling below
├── server/
│   ├── fakelib/
│   │   └── generate_library.py # builds the fake /Photos tree from real sample JPEGs
│   ├── docker-compose.yml      # PhotoView + SQLite, mounts server/data/photos read-only
│   └── data/
│       └── photos/             # generated fake library (gitignored) — stands in for the NAS volume
└── client/
    ├── sync_client.py          # minimal GraphQL sync-proof script
    └── cache/                  # downloaded thumbnails + local sync state (gitignored) — stands in for on-device storage
```

This split is deliberate groundwork for the real thing: everything under `server/` is what
would live on NAS (Docker, the photo volume, the DB) and everything under `client/` is what
would live on a phone/laptop (thumbnail cache, local sync state) — so the eventual real PWA's
`client/` code has an obvious home next to this prototype's.

## What gets built

### 1. Fake library generator (`server/fakelib/generate_library.py`)

Rather than synthetic solid-color images, this uses **real sample photos** so PhotoView's
thumbnailer/EXIF handling is exercised against genuine JPEG data:

- Source: a read-only copy from
  `/path/to/sample-photos`
  (56 real JPEGs, Google Photos Takeout export with `.supplemental-metadata.json` sidecars
  carrying real `photoTakenTime` timestamps). **Originals in that folder are never modified**
  — the generator only reads from there and copies into `server/data/photos/`.
- For each selected photo: copy it into the target tree under a normal camera-style filename
  (e.g. `IMG_3312.JPG`, not the takeout-style `07-04-09-07 Holliday...`), and write a real
  EXIF `DateTimeOriginal` tag derived from the sidecar's `photoTakenTime` (via `piexif`),
  adjusted where needed to construct the specific time-gap patterns below. No GPS is written
  (the source sidecars carry no usable GPS, and geocoding is dropped anyway).
- Time patterns constructed across the 56 photos to exercise the gap-based grouping logic:
  a few same-day bursts, a few multi-day gaps (new moment), and **one deliberate moment that
  spans a month boundary and one that spans a year boundary**, to verify the "no forced split"
  decision holds and PhotoView's timeline still reads correctly across that boundary.
- Groups the ~56 photos into moments using dad's gap logic (2-day default), reused as a
  small extracted function rather than copy-pasted, and writes them directly into
  `server/data/photos/<user>/YYYY/YYYY-MM-DD-moment/` — i.e. **the generator produces
  already-sorted output**, so this pass tests PhotoView's scanning of the target layout, not
  the sorter itself.
- Creates 2 separate user root directories (`server/data/photos/alice/`,
  `server/data/photos/bob/`, splitting the 56 photos between them) to exercise PhotoView's
  per-user path isolation.
- `pyproject.toml` at the repo root, managed with `uv` (`uv run server/fakelib/generate_library.py`),
  with `Pillow` and `piexif` as dependencies. No global pip installs.

### 2. PhotoView via Docker (`server/docker-compose.yml`)

- Official `photoview/photoview` image (arm64/amd64 — matches both this Mac and the eventual
  DS124 target).
- Mounts `server/data/photos` read-only into the container as the scan root, matching how it'd
  be mounted on DSM.
- SQLite (not Postgres) for the database, since DS124 is single-drive/1GB and the real
  deployment will likely also want SQLite rather than running a second heavy service.
- Explicit container memory limit (`mem_limit: 1g`) so `docker stats` output approximates the
  real DS124 constraint, even though this runs on a Mac.
- After `docker compose up`, manually exercise: initial scan time, thumbnail generation,
  timeline view (checking the month/year-boundary moments render as single continuous
  albums), creating the 2 users each scoped to their own path, and toggling favorites —
  noting memory/CPU usage from `docker stats` throughout.

### 3. Sync client prototype (`client/sync_client.py`)

A minimal script (not the real PWA) that proves the sync design against PhotoView's actual
API, using `uv run client/sync_client.py <command>`:

- Logs in via PhotoView's GraphQL auth mutation, stores a token in `client/cache/`.
- Full sync: pulls `myAlbums`/`myTimeline`, downloads thumbnail bytes for each media item into
  `client/cache/thumbnails/` (standing in for IndexedDB/on-device storage), records a local
  `last_synced_at` timestamp in `client/cache/state.json`.
- Incremental sync: re-run pulls `myTimeline(fromDate: last_synced_at)`, downloads only new
  thumbnails, and separately confirms via a full `myAlbums` re-list that nothing was deleted
  in between (proving out the "date filter + periodic reconciliation" approach).
- Toggle a favorite via mutation, then verify it's reflected in a subsequent sync — proving
  favorites propagate through the same mechanism as photos.
- Demonstrates the "original on device" shortcut conceptually: the script marks which cached
  items are "local originals" (simulating photos captured by that device) vs. "thumbnail
  only," and its fetch-original logic checks the local flag before ever hitting the server —
  proving the client-side shortcut logic without needing a real phone camera roll.

## Explicitly out of scope for this pass

- No real client UI/PWA — the sync client is a CLI script proof, not React/IndexedDB yet.
- No album creation / folder-move / rename-via-app — read/sync/favorite only.
- No Tailscale, no real DS124/DS112, no real Synology Photos integration.
- No upload path.
- No reuse of dad's geocoding — dropped per the decisions above.
- No `synoindex -N`-safe move logic yet — flagged above as required before any real sorter
  touches a live Synology-Photos-backed library, but not needed against the fake local library.

## Verification — results from running this end-to-end

- `uv run server/fakelib/generate_library.py --clean` produces `server/data/photos/{alice,bob}/`
  from 97 real JPEGs (piexif only writes EXIF into JPEG containers, so the source folder's
  PNG/HEIC files are skipped), grouped by the gap logic into 40 moments including a deliberate
  month-spanning and year-spanning moment. Confirmed via `piexif.load()` that written
  `DateTimeOriginal` values match the intended synthetic timeline exactly.
- `docker compose -f server/docker-compose.yml up -d` brings up PhotoView 2.4.0 cleanly with
  `PHOTOVIEW_DATABASE_DRIVER=sqlite` — no separate DB container needed. Initial setup requires
  `initialSetupWizard(username, password, rootPath)`, then `createUser` + `userAddRootPath` for
  the second user (the schema's `createUser` does **not** accept `rootPath` directly, despite
  `master`-branch docs suggesting otherwise — version drift between `:2` and `master`).
  **Auth is cookie-based** (`Cookie: auth-token=<token>`), not `Authorization: Bearer` as
  `master`'s docs implied — the bearer path is websocket-subscription-only in this version.
- **Resource usage (97 photos, full scan incl. thumbnailing + face detection + blurhash)**:
  peaked ~470MiB during face detection, settled at a steady-state **449.8MiB / 1GiB (44%)**
  once idle. Scan completed in ~35 seconds. This is a solid, comfortable margin under the
  DS124's 1GB total (which also has to run DSM itself) — a strong positive signal, though this
  ran on a Mac and 97 photos is far short of the real ~28,000/user target, so this is not yet
  a conclusive answer on the full-scale library. Recommended next step before fully committing:
  re-run this same test at 1,000-5,000 photos to see how memory scales, ideally on real ARM
  hardware.
- **Albums correctly mirror the folder tree 1:1** and **the timeline correctly reads
  boundary-spanning moments as single continuous groups** — confirmed directly against bob's
  `myTimeline`: `2024-06-30-moment`'s photos (Jun 30 → Jul 3) and `2024-12-30-moment`'s photos
  (Dec 30 → Jan 1) each appear grouped under one album, validating the no-force-split decision.
  Two users only see their own scoped root path.
- **Important correction to the original sync-model assumption**: `myTimeline(fromDate: ...)`
  is **not** a changes-since filter — confirmed directly against the live server, it returns
  items with EXIF capture date ≤ fromDate (a chronological pagination cursor), so a request
  with `fromDate` = "last sync time" returned 32 old items, not 0 new ones. **The sync client
  was corrected to do a full `myAlbums` re-list every sync**, diffing returned media IDs
  against a locally-cached ID set to decide what needs a thumbnail download — this is what's
  actually implemented in `client/sync_client.py` now, not the fromDate-filtered approach
  originally planned.
- `uv run client/sync_client.py login bob ...` then `full-sync` downloaded all 62 of bob's
  media items' real thumbnail JPEGs into `client/cache/thumbnails/`. A subsequent
  `incremental-sync` correctly found 0 new/0 removed after no server-side change. Toggling a
  favorite (`favorite 45 on`) round-tripped and was independently confirmed via
  `myAlbums(...).media(onlyFavorites:true)`. Deleting a photo file from disk, triggering
  `scanAll`, then running `incremental-sync` correctly detected 1 removed item and deleted its
  cached thumbnail. The "original already on device" shortcut (`open`/`mark-local` commands)
  checks local state before any network call, as designed.

**Operational note**: if `server/fakelib/generate_library.py --clean` is re-run while
PhotoView's container is already up, the container can end up with a stale view of the
bind-mounted `/photos` directory (`scanAll` fails with "Album directory ... does not exist"
even though it exists on the host) — `docker compose restart photoview` fixes it. Worth
regenerating the library before first bringing the container up, not while it's running.

**Second operational note, found later**: `--clean`-regenerating the *fake library* does not
clean *PhotoView's own database* — after enough rename/merge/rescan test cycles, PhotoView's
SQLite DB can accumulate orphaned albums that no longer match anything the current library
would produce, and a plain `scanAll` does not garbage-collect them (moments that legitimately
went missing stayed missing across multiple regenerate+restart+rescan cycles). The reliable
reset is a full wipe: `docker compose down`, `rm -rf server/data/photoview`, `mkdir -p
server/data/photoview/{media-cache,database}`, `docker compose up -d`, then redo
`initialSetupWizard`/`createUser`/`userAddRootPath` from scratch (see server/docker-compose.yml
and the setup calls used throughout this doc). This is a prototype-session artifact from lots
of manual rename/merge testing in place, not something a real deployment would hit the same
way — but worth knowing the fix rather than debugging stale album lists as if they were new bugs.

## Memory: what's actually resident, and how it scales

Instrumented directly via `/proc/1/status` inside the container (`docker stats` alone
conflates this with page cache and isn't precise enough).

- **Idle steady-state: ~88MB RSS (main process) + ~19MB (persistent `exiftool -stay_open`
  helper) ≈ 107MB total**, holding rock-steady over a 2-minute observation window with zero
  drift. Nothing photo-count-related is held resident at idle — paths/EXIF/thumbnails all live
  on disk (SQLite + thumbnail files) and are read on demand, not cached in the app's own
  memory as a growing structure.
- **Client browsing load** (repeated album/timeline queries, fetching every thumbnail's image
  bytes, fetching every full-resolution original — some concurrently): RSS moved **88MB →
  97MB** and stayed there. Both thumbnails and originals are pre-generated/already-on-disk
  files served directly, no per-request decode — so browsing, even heavy concurrent browsing,
  does not meaningfully spike memory.
- **Scanning new/changed photos is the only path that spikes memory.** Scanning 62
  never-before-seen photos (decode + thumbnail generation + blurhash + face detection) peaked
  at **284MB RSS**, then fell back to ~130MB shortly after. This scales with concurrent
  in-flight decode work during a scan, not with total library size — a library of 30,000
  already-scanned photos sitting idle costs the same ~107MB as a library of 100.
- **Practical implication**: the steady-state number is very comfortable on a 1GB device; the
  number actually worth stress-testing is the **scan-time peak**, since real phone photos
  (4-5MB) will decode to larger buffers than this test's ~200-400KB samples, and the peak will
  be worse if periodic scans overlap with a face-detection backlog (see below).

### Scale test: ~2,100 real, full-size photos (with face detection already disabled)

Ran the same `/proc/1/status` measurement against 2,000 real photos copied from an actual
Synology-Photos-managed backup (`namasenda`'s phone camera roll, real EXIF, average ~4.5MB/file,
9.02GB total — see `server/fakelib/generate_scale_test_library.py`), added on top of the
existing 97-photo library, for ~2,097 total. This directly answers next-step #3+#4 (scale and
real file size) in one test, and with face detection already off, isolates the
thumbnail/blurhash/decode cost alone.

- **Scan duration: ~10 minutes** for ~2,097 photos (vs. ~35 seconds for 97 small archival
  samples) — a real, useful data point: scan time scales with photo count and file size, not
  something to hand-wave away for a real ~28,000-photo/user library (which would extrapolate
  to multiple hours for a first full scan, though incremental scans of only new photos would be
  far shorter in normal operation).
- **Memory stayed bounded throughout**: peak RSS observed **~136MB**, most of the scan spent
  oscillating in the 80-135MB range (a visibly different, gentler curve than the 97-photo
  test's single sharp 284MB spike — larger files but no face detection this time, and the
  scanner appears to process/release memory per-photo rather than accumulating). Settled to
  **~125MB RSS post-scan**.
- **This is the strongest result yet for the 1GB DS124 target**: real file sizes, 20x the
  previous photo count, and memory never approached even a quarter of the 1GB limit. Combined
  with the earlier finding that steady-state memory doesn't scale with library size at rest,
  this meaningfully de-risks PhotoView as the backend choice — next-step #3 and #4 are now
  answered; #6 (actual DS124 hardware, not this Mac) remains the one unverified variable
  (different CPU architecture, and DSM itself competing for the same 1GB).

## Face detection: disable it

Confirmed via source inspection (not schedulable, so "run it only overnight" isn't an option
PhotoView supports):

- Face detection is CPU-only (dlib via `go-face`), runs on the **already-generated thumbnail**
  (not the full-res original, so it's cheaper than it sounds), but is wired synchronously into
  the same per-photo scan pipeline as EXIF/thumbnail/blurhash — there is no separate schedule,
  queue, or off-peak trigger for it. The only control is on/off.
- **Set `PHOTOVIEW_DISABLE_FACE_RECOGNITION=1`** in `server/docker-compose.yml`. This also
  hides the "People" UI entirely. Given the ask was "only if cheap to run overnight," and
  overnight-only isn't achievable, disabling is the right call for now — revisit if PhotoView
  later adds scan-phase scheduling, or if a real-scale test shows it's cheap enough to leave on
  anyway.

## Critical finding: renaming a file breaks its identity

Tested directly against the running server: favorited a photo (media id `1`), renamed its file
on disk with a plain `mv` (no PhotoView API involved — the scenario a real Synology-Photos
upload + later reorganization would hit), triggered `scanAll`.

**Result: the rename was not detected as a rename.** The old media id (`1`) was deleted
entirely (`could not get media by media_id ...: record not found`) and a brand-new id (`161`)
was created for the file at its new name, with **`favorite` reset to `false`**. PhotoView has
no content-hash or filename-independent identity — it is purely path-keyed, exactly like the
`synoindex` behavior already flagged in the Synology Photos section above.

**Consequences for the real design:**
- Favorites, face labels, share links, and any other per-media state are tied to an ID that a
  raw rename/move invalidates. **Any real sorter or reorganizer must never rename/move files
  behind PhotoView's back** — it would silently blow away user-applied stars.
- This directly threatens the "user renames the moment folder from `2024-03-10-moment` to
  `2024-03-10-LeedsWithDad`" idea the folder-naming decision earlier in this plan relies on —
  **confirmed by testing it directly**: renamed `2024-03-10-moment` → `2024-03-10-LeedsWithDad`
  on disk (with `IMG_1002.JPG` pre-favorited as id `2`), rescanned. Result: the old album
  disappeared from `myAlbums` entirely, a brand-new album appeared at the new name, every
  contained photo got a new media id (`2` → `164`, etc.), and the favorite was lost
  (`could not get media by media_id ...: record not found` for the old id). **A folder rename
  is exactly as destructive as a single-file rename** — this is a confirmed blocker for the
  "rename moments in the app" UX as currently imagined, not a hypothetical risk.
- The safe pattern going forward: any operation that changes a file's path (through the future
  app's own "rename album" or "move photo" features) should go through PhotoView's own
  mutations if it has path-aware ones (needs checking — not yet confirmed whether PhotoView
  exposes a move/rename mutation, or only re-scans), or the app must independently snapshot
  and restore favorite/face state around any move it performs.

## Solved: moment rename/merge/split, with favorites preserved

Confirmed via source inspection (see next paragraph) that PhotoView has no move/rename-aware
mutation and never will without a schema change — matching purely by `path_hash = MD5(path)`
in both `scanner_album.go` and `scanner_media.go`, with no content-hash/size/mtime fallback,
acknowledged by a maintainer on [issue #1148](https://github.com/photoview/photoview/issues/1148)
as "it doesn't see a difference between [rename vs. new file]." This is structural, not a gap
that a future PhotoView version is likely to close incidentally.

Decision: **`server/appdb`** (new) is a small SQLite database, keyed by **content hash** (not
PhotoView's own id, confirmed unstable), that becomes the actual source of truth for favorites
and moment labels — matching the original design doc's own intent ("stars stored centrally,"
"application database is disposable, rebuild from filesystem"). PhotoView's own `favorite`
column is kept as a synced read-optimization for its own UI/timeline filtering, never the
source of truth. This was a deliberate choice over two alternatives considered and rejected:
directly patching PhotoView's SQLite tables (couples us to an undocumented, version-fragile
schema — the opposite of "PhotoView's DB is disposable"), and a fingerprint-based
reconciliation using EXIF/filename matching after the fact (unreliable for moments by
definition, since files in the same moment often share timestamps/similar names).

**`server/reorganize.py`** implements `rename_moment`, `merge_moments`, and `move_photos` (the
last is also the building block for "adjust a moment's date boundary" — a split is just moving
the photos on one side of the new boundary into a fresh adjacent moment). Each operation:
moves the real files on disk (the filesystem stays authoritative), updates `appdb` for the
moved content hashes immediately (app state is correct before PhotoView even knows anything
changed), triggers `scanAll` and **waits for the moved paths to actually appear** in
`myAlbums` (a real bug caught during testing: `scanAll` returns as soon as the scan is queued,
not when it's done — reconciling favorites immediately after was silently a no-op the first
time this was tested), then replays `favoriteMedia` onto the new media ids for anything
`appdb` says should be favorited.

**All three operations tested directly against the running PhotoView instance and confirmed
working**: renamed a moment with a pre-favorited photo (new PhotoView id, favorite correctly
restored), merged two moments into a new "spain-holiday" moment with a pre-favorited photo
inside one of the sources (favorite correctly followed the photo into the merged moment under
its new id), and split a moment by moving one photo into a newly-created adjacent moment
(same result). A second real bug was caught and fixed during this testing: `scanAll` is
admin-only, but `favoriteMedia`/`myAlbums` are scoped to the owning user's own session — the
first version of `reorganize.py` used a single token for both and failed for any user other
than the one the admin account happens to be scoped to. Every operation now takes `admin_token`
(triggers the scan) and `user_token` (reconciles that user's own favorites) as distinct
parameters.

**Known remaining gap**: `appdb` only becomes aware of a favorite if it was set through
`reorganize.set_favorite()` (which writes appdb first, then mirrors into PhotoView) — a
favorite toggled directly through PhotoView's own UI/`favoriteMedia` bypasses appdb entirely
and will be silently lost on the next rename. `reorganize.import_favorites_from_photoview()`
exists to backfill appdb from PhotoView's current state, but nothing calls it automatically
yet. **The real app's favorite-toggle UI must always go through the app's own write path
(appdb first), never call PhotoView's `favoriteMedia` directly** — this is a hard requirement
for the design, not a nice-to-have, now that it's been demonstrated to actually break things
when violated (this exact mistake happened during interactive testing and produced a
real reproduction of the failure).

**Decision confirmed with the user: all file moves/renames go through the app, never manually
by the user.** This removes the harder version of the durability problem (reconciling against
surprise external filesystem changes) and means appdb can stay authoritative in real time.
Given that, a **JSON sidecar export** was added as the long-term portability layer, matching
the original design doc's own `/Photos/.metadata/stars.json` idea almost exactly:
`appdb.export_stars_json()` writes `server/data/photos/.metadata/stars.json`
(content-hash-keyed, human-readable), refreshed automatically on every favorite change and on
every path update for an already-favorited photo (so a rename/merge/split keeps the JSON's
`path` field accurate, not just its favorite-set membership). appdb (SQLite) remains the
fast, real-time source of truth the app actually reads/writes during normal operation; the
JSON is the "outlives the app" copy — readable with nothing but a text editor if PhotoView,
appdb, and this whole codebase are all gone. Tested directly: favorited a photo, confirmed it
appeared in `stars.json`; renamed its moment folder; confirmed the JSON's `path` field updated
to the new location while the content-hash key (and PhotoView's own restored favorite) stayed
correct.

EXIF/XMP embedding (writing favorite status into the image's own metadata) was considered and
explicitly rejected for now: it mutates the original file's bytes on every favorite toggle,
which cuts against "originals must remain independent/untouched" and would invalidate any
future content-hash-based dedup every time a star is toggled. Not needed anyway, since RAW
(where XMP sidecars are the norm) isn't expected to be a real case for this library.

## Confirmed: Synology Photos does not use per-file `@eaDir` thumbnails

Checked directly on the real NAS NAS via SSH (bypassing SMB, which hides `@eaDir` from any
client regardless of whether it exists — see the Synology Photos section above for how that
was discovered). `ls @eaDir/` on a real Synology-Photos-uploaded folder
(`Photos/MobileBackup/iPhone/2026/08/`) showed only `<filename>` and `<filename>@SynoEAStream`
entries — extended-attribute metadata, not `SYNOPHOTO_THUMB_*`/`SYNOFILE_THUMB_*` image files.
A `find` for photo-related paths located Synology Photos' real cache/database at
**`/volume1/@appdata/SynologyPhotos`** — a centralized, per-package store (its own `synofoto`
database and log files), not a per-file sidecar. **Practical implication: a real mover
reorganizing Synology-Photos-uploaded content needs no thumbnail-dragging logic at all** —
dad's script's `move_thumbnail()`/`has_thumbnail()` functions target a mechanism
(`@eaDir/<filename>/SYNOFILE_THUMB_*`) that doesn't apply to this content. The `find` also
surfaced a separate, older `/volume1/photoalbums/` + `/volume1/@eaDir/photoalbums` structure
(distinct from the newer `homes/*/Photos` tree) where legacy `@eaDir` thumbnails may genuinely
apply — not the tree this project is modeled on. The remaining real problem for the
Synology-Photos-managed tree is `synofoto` database desync (already covered above under
"Synology Photos interaction"), which is a metadata-sync problem, not a lost-thumbnail one.

## Not yet tested: "client already has this photo" dedup

Not tested this pass. The real upload path is Synology Photos auto-backing-up from a phone,
independent of this prototype's fake library. Two separate questions worth separating:

1. **Does PhotoView (or the future app) recognize a photo the client already captured/uploaded,
   to avoid the client re-fetching an original it already has locally?** The prototype's
   `client/sync_client.py` already models the intended mechanism for this (the `local_originals`
   list + `open`/`mark-local` commands) — it's a client-side responsibility, not something
   PhotoView needs to solve. In the real app this would be keyed off some stable identifier the
   phone already knows for a photo it took (e.g. its own local asset ID or a content hash),
   matched against whatever PhotoView returns for that media item. Given path-keyed identity is
   confirmed fragile (see rename finding above), a **content hash** (e.g. SHA-256 of the file,
   or even reusing dad's script's checksum idea from the design doc) is the safer matching key
   than PhotoView's own `id`, which is not stable across reorganization.
2. **Does Synology Photos itself dedupe against what it already uploaded, if the phone later
   asks "should I upload this"?** This is the earlier-researched, separate, already-answered
   risk (see "Synology Photos interaction" above) — path-keyed matching, real duplicate-upload
   failure mode, mitigated by `synoindex -N` rather than raw moves. Not re-tested this pass;
   flagged as a Phase 3+ concern since this prototype doesn't touch Synology Photos.

## Go/no-go signal from this pass

PhotoView is a credible backend candidate: clean SQLite-only deployment, correct folder→album
mirroring, a working (if not delta-based) timeline, working per-user isolation, and very
comfortable idle memory headroom regardless of library size. The changes-since sync API
assumption it disproved was absorbed into the client without abandoning PhotoView. **The
confirmed blocker is path-keyed identity breaking on any rename or move** — both single-file
and whole-folder renames were tested directly and both destroy the media id and any favorite
tied to it. This doesn't rule out PhotoView, but it rules out doing renames/moves as a raw
filesystem operation from either this prototype's future sorter or the app's planned
"rename moment" feature — see next steps.

## Next steps

1. **Design around the confirmed rename-breaks-identity finding** before building the "rename
   moments in the app" feature: either (a) have the app snapshot favorites/face-labels before
   any move/rename it performs and reapply them by matching on a content hash afterward, or
   (b) check whether PhotoView exposes any move/rename-aware mutation (not yet found in the
   schema surface used so far — only `scanAll`/`scanUser` react to filesystem changes) that
   would let the app tell PhotoView about a rename instead of it inferring one from a rescan.
   This is now the most important open question, not a nice-to-have.
2. **Disable face detection** in `server/docker-compose.yml`
   (`PHOTOVIEW_DISABLE_FACE_RECOGNITION=1`) — decided above: not schedulable, so off is the
   right default for now. Confirm the scan-time memory peak drops with it removed.
3. ~~**Scale test**~~ — done. See "Scale test: ~2,100 real, full-size photos" above: ~2,097
   real photos (avg 4.5MB), ~10 minute scan, peak RSS ~136MB, settled ~125MB. Comfortable margin.
4. ~~**Real-size-photo test**~~ — done in the same pass as #3 above (real 3-7MB phone photos
   from an actual Synology-Photos-managed backup, not synthetic samples).
5. **Client-side dedup design**: since PhotoView's own `id` is confirmed unstable across
   reorganization, design the "does the client already have this photo" check (see "Not yet
   tested" section above) around a content hash rather than PhotoView's `id` from the start,
   rather than discovering the same instability there later. Still open — the web client's
   `client/web/src/db.ts` currently keys its IndexedDB cache by PhotoView's own media id, same
   gap noted below.
6. **Real hardware**: repeat the idle/scan/browsing memory measurements on the actual DS124 (or
   at least an ARM box with 1GB RAM) rather than this Mac, since CPU architecture and available
   RAM headroom (DSM itself needs some) differ meaningfully from a dev machine. Still the one
   unverified variable — everything above was measured on Apple Silicon, not the DS124's
   Realtek RTD1619B.
7. ~~Move to Phase 2 (the real PWA client)~~ — started; see "PWA client: first working slice"
   below for what's built and proven so far, and what's still missing.
8. ~~**Client-side scale test**~~ — done. See "Client-side scale test: ~2,030 real photos synced
   into the browser" below: 58.2s full sync, IndexedDB correctly holds all 2,030 rows, JS heap
   stays flat (10MB used) regardless of scroll position. Scroll-frame-timing result is
   inconclusive/needs a better methodology (see that section) — flagged as a follow-up, not a
   pass/fail.
9. **Proper continuous-scroll performance measurement** — the discrete-jump scroll test above
   likely overstates frame cost vs. real smooth scrolling; worth re-measuring with
   `page.mouse.wheel()` in a loop before deciding whether virtualizer tuning is actually needed.

## Future UI requirements (noted for later, not yet built)

Captured directly from the user after seeing the first working PWA build, for when UI work
resumes beyond the current bare-bones prototype:

- ~~**Starred-only toggle**~~ — done. See "Starred-only toggle + IndexedDB favorite-field fix"
  below.
- **Full-screen photo viewer** — click a thumbnail to open it full-size, with left/right
  navigation between photos (not just the grid view that exists today).
- **Adjustable grid density** — a control to change photos-per-row (e.g. 3 vs. 20), i.e. the
  virtualizer's fixed `columns = 6` in `Timeline.tsx` needs to become a user-adjustable setting
  rather than a hardcoded constant.
- **Restructured layout**: a left-hand panel for login/account and sync status, rather than the
  current top header bar.
- **Sync should be automatic, not a manual button** — triggered on app open, with rename/reorg
  actions similarly ambient rather than requiring an explicit visible "Sync" click. The
  long-term intent is that a user should rarely if ever need to see or think about syncing —
  the current explicit Sync button is a prototype/testing convenience, not the intended final UX.

## Starred-only toggle + IndexedDB favorite-field fix

First of the "Future UI requirements" items built. User specifically flagged, while reviewing
the code, that `favorite` should be an indexed IndexedDB field before this feature landed,
rather than filtering the whole cached library in JS on every toggle — right call, and
implementing it surfaced a real bug worth the extra care.

**The bug**: `CachedPhoto.favorite` was typed as `boolean`. IndexedDB's native key types
(`IndexableType`) do not include `boolean` at all — only `string | number | Date |
ArrayBuffer | ...` — so a boolean-typed field can't be used with
`.where('favorite').equals(...)` at all; TypeScript itself caught this at build time
(`Argument of type 'boolean' is not assignable to parameter of type 'IndexableType'`). Fixed
by changing the field to `0 | 1`, matching `server/appdb`'s existing `favorite INTEGER` column
for the same underlying reason.

**The harder bug this surfaced**: adding the index via `db.version(2).stores({photos: '...,
favorite'})` alone does **not** retroactively fix the *values* already sitting in existing
rows from `version(1)` — those still hold real `true`/`false` booleans, which (confirmed by
direct testing, not assumed) are silently excluded from every `.where('favorite').equals(1)`
query even though they're still present in a plain `.toArray()`/`.getAll()` scan. Reproduced
by seeding a browser's IndexedDB directly at schema v1 with boolean-shaped rows, then loading
the real app so its own Dexie instance triggers the v1→v2 upgrade - without a migration step,
`legacy-1` (favorite: true) never appeared in `.where('favorite').equals(1)` results after the
"upgrade." Fixed by adding a `.upgrade()` callback to the `version(2)` declaration that walks
every existing row and normalizes `favorite` to `0`/`1` before the new index is live. Re-ran
the same reproduction after the fix and confirmed `legacy-1` now correctly appears in the
index and `legacy-2` (favorite: false) correctly does not.

This only matters for anyone who used the app before this fix landed and didn't clear/re-sync
their local cache - a small blast radius right now, but the kind of silent-data-exclusion bug
that's much cheaper to catch and fix during a solo prototype than after real deployment.

**UI**: a "★ Starred only" checkbox in `App.tsx`'s header, wired to a new `starredOnly` prop
on `Timeline`, which switches its `useLiveQuery` between `db.photos.where('favorite').equals(1)`
and the existing full `orderBy('date')` scan.

## PWA client: first working slice

Built `client/web/` — React 19 + Vite + TypeScript, chosen after researching how real
self-hosted photo apps solve the hardest part of this UI (see below) rather than assuming
React was still the right call once the original design doc's spec was set aside. Stack:

- **`@tanstack/react-virtual`** for virtualized rendering — chosen after research showed
  PhotoView's own React frontend does **no virtualization at all** (just `.map()`s every
  fetched photo into the DOM), and that Immich and PhotoPrism (both larger, more mature
  self-hosted photo apps) hand-roll their own bucketed-scroll virtualization rather than using
  a library, because no library ships true justified/photo-grid-aware virtualization out of
  the box. TanStack Virtual was picked over Svelte's `virtua` specifically for having more
  prior art/documentation for a solo maintainer to lean on later — the deciding factor once it
  was clear the framework itself (React vs. Svelte vs. Solid) mattered less than this library
  choice for an app that's mostly "one big scrollable grid, little other state."
- **Dexie.js** as the IndexedDB wrapper — the on-device thumbnail cache the whole "local
  timeline" design depends on.
- **`vite-plugin-pwa`** for the service worker/manifest — configured to precache only the app
  shell (JS/CSS/HTML), not photo bytes (those are already cached deliberately via Dexie, not
  the service worker's cache).
- **`graphql-request`** — a thin GraphQL client, not full Apollo, since this app's server
  interaction is simple enough not to need Apollo's cache/normalization machinery.

**Timeline component** (`src/Timeline.tsx`) buckets cached photos by moment (mirroring
PhotoView's own album grouping) and virtualizes the resulting rows with TanStack Virtual —
essentially Immich's proven "bucket by time period, virtualize within/across buckets" pattern,
just at coarser (moment-level, not month/day) granularity, matching the "one year in memory at
a time" idea from the original discussion.

**Two real integration bugs found and fixed by actually running the app end-to-end** (not
caught by type-checking or by reasoning about the API alone):
1. `graphql-request`'s `GraphQLClient` needs an absolute URL even for a same-origin request —
   a bare `/api/graphql` throws `Failed to construct 'URL': Invalid URL`. Fixed by building
   the endpoint from `window.location.origin`.
2. **PhotoView sets no `Set-Cookie` header on `authorizeUser`** (confirmed directly via
   `curl -D-`, matching the same finding already documented for `client/sync_client.py`) — the
   browser client has to read the `token` field from the mutation response and manually set
   `document.cookie` itself. Without this, every subsequent query correctly succeeds at the
   network level but returns `{"errors":[{"message":"unauthorized"}]}` — this looks like an
   auth bug in the query, not a missing cookie-setting step, unless you already know PhotoView's
   token model, which is why this was worth writing down.

**CORS**: PhotoView sends no `Access-Control-Allow-*` headers at all (confirmed via an OPTIONS
preflight), so the browser blocks direct cross-origin requests from Vite's dev server
(`localhost:5173`) to PhotoView (`localhost:8000`). Fixed with a Vite dev-server proxy
(`/api` → `http://localhost:8000`) rather than trying to configure CORS on PhotoView's side —
this also reasonably approximates how a real deployment would likely serve the PWA behind the
same reverse proxy as PhotoView anyway, so it's not purely a dev-only workaround.

**Verified working end-to-end** by driving the actual running app with Playwright (headless
Chromium) rather than just type-checking: logged in as admin, clicked Sync, watched 500
thumbnails download via real network requests, and confirmed the timeline renders real photos
grouped under moment headers with zero console errors. Screenshot evidence retained during
the session (not committed to the repo).

**Known gaps at that point** (since resolved — see "Full client-side journey" below):
IndexedDB keyed by PhotoView's unstable `id`, no favorite-toggle UI, no HTTP API in front of
`reorganize.py`, no offline verification.

## Full client-side journey: favorite + rename UI, HTTP API, and end-to-end offline test

Goal for this pass: prove the complete user journey works locally before ever touching NAS —
login → sync → browse → favorite → work offline → rename a moment → re-sync → confirm nothing
was lost. This required real new work, not just testing: `reorganize.py`/`appdb` had no HTTP
surface for the browser to call, and the UI had no favorite or rename controls yet.

**`server/api.py`** (new) — a small FastAPI service in front of `reorganize.py`/`appdb`:
`POST /favorite`, `POST /moments/rename`, `POST /moments/merge`, `GET /health`. Auth model: the
browser's own PhotoView session cookie is forwarded as the `user_token` for user-scoped
operations (matches `reorganize.py`'s existing `user_token` parameter); the API server holds
its own cached admin credentials internally (env-var configurable, defaulting to the prototype
test admin) for the one admin-only call (`scanAll`), so the browser session itself never needs
admin rights. Paths from the client are resolved against and validated to stay under
`PHOTOS_ROOT` — restating the original design doc's own security principle ("never accept
arbitrary filesystem paths from the client") for a client that, unlike the doc's original
photo_id/album_id scheme, does currently pass real relative paths, since content-hash/moment-id
lookups aren't wired up client-side yet (still an open gap, see below).

**Client additions**: `src/api.ts` (client for the new FastAPI service), a favorite-star
button overlaid on each thumbnail in `Timeline.tsx` (writes through `appdb` via `/favorite`,
never `favoriteMedia` directly — the hard constraint from the rename-breaks-identity finding),
and an inline rename form on each moment header (calls `/moments/rename`).

**Two more real bugs found by testing, not by reasoning about the code**:
1. FastAPI's `Cookie(default=None)` parameter needs an explicit `alias="auth-token"` — without
   it, FastAPI looks for a cookie literally named `auth_token` (Python parameter name), not
   PhotoView's actual `auth-token` cookie, and every request silently came back "not logged in"
   even though the browser was sending the right cookie under the right name.
2. **`vite-plugin-pwa`'s service worker is disabled by default under `vite dev`** — testing
   offline behavior against the dev server always failed
   (`net::ERR_INTERNET_DISCONNECTED` on reload) regardless of what the PWA config said, because
   no service worker was ever registered in dev mode in the first place. Real offline testing
   needs a production build (`npm run build`) served via `npm run preview` — `vite preview`
   also reads a *separate* `preview.proxy` config from `server.proxy`, so both had to be kept in
   sync in `vite.config.ts` for the proxied API calls to keep working under preview too.

**Full journey test** (Playwright driving the production preview build, not the dev server):
login → sync (35 photos) → favorite a photo → confirm the service worker is active → go fully
offline → reload → **confirm the timeline still renders real cached photos with zero network**
→ come back online → rename a moment via the UI → re-sync → confirm the renamed moment appears
and the favorite survived under its new PhotoView id. All steps passed, zero console errors
throughout. This is the first real confirmation that the PWA/offline story — the whole point of
"keep a local thumbnail cache" — actually works, not just that the service worker config looks
right on paper.

**One test-authoring lesson worth keeping**: the first run of this test silently mis-clicked —
`page.fill('form input', ...)` matched the *login form's* username field instead of the rename
form's input, because both forms contain a bare `<input>` with no distinguishing selector. The
app behaved safely under the resulting no-op rename (renaming a folder to its own existing
name), but the bug cost a full debug cycle. Fixed by adding `data-testid` attributes
(`login-status`, `sync-status`, `rename-input`) to the real app's DOM rather than relying on
structural/text selectors — cheap to add now, and the same ids are reusable for any future
automated testing of this UI, not just this one-off script.

**Remaining known gaps**:
- IndexedDB cache (`client/web/src/db.ts`) is still keyed by PhotoView's media `id`, not a
  content hash — the journey test's re-sync step worked because a full re-list + ID-diff
  correctly treats a renamed photo's new id as "new" and downloads it again (no orphaned data,
  just a redundant download), but a true content-hash-keyed cache would recognize the same
  photo and skip the re-download. Not a correctness bug, just an efficiency one left for later.
- No merge-moments UI yet (the API endpoint exists and needs the same journey-test treatment).
- Login has no session persistence — reloading the page requires logging in again every time
  (the journey test does this explicitly at its offline-reload step). A real version needs an
  "am I already logged in" check on mount, probably backed by a longer-lived stored token.
- The FastAPI admin-token cache (`server/api.py`) never re-authenticates on failure/expiry —
  fine for this prototype's session-length testing, not fine long-term.

## Client-side scale test: ~2,030 real photos synced into the browser

User-requested (see "Future UI requirements" below for the other feedback from this same
session) before moving further into UI polish: does the actual React client — not just the
server — stay responsive at real scale? Reused the same 2,000-real-photo set from the earlier
server-side scale test (`server/fakelib/generate_scale_test_library.py`, real `YYYY/MM`
folders from an actual phone backup, no fake-library generator needed) plus the existing
97-photo library, added as a new PhotoView root (`/photos/scaletest`) alongside `alice`.

**A real client bug surfaced immediately**: `fetchTimeline()` was hardcoded to a single
`myTimeline(paginate:{limit:500})` call — fine at the ~35-500 photo scale tested so far, but
silently truncated to the first 500 of 2,030 photos with no error. Fixed with proper
`limit`/`offset` pagination, looping until a page returns fewer than `pageSize` results (see
`src/photoview.ts`). Confirmed the underlying pagination behaves correctly first via a direct
`curl` GraphQL call (two 5-item pages, no overlap) before trusting the client-side loop.

**Results** (Playwright driving the production preview build, PhotoView pre-scanned):
- **Full sync of all 2,030 photos: 58.2 seconds**, downloading every thumbnail not already
  cached, zero errors. IndexedDB confirmed to hold all 2,030 rows afterward.
- **JS heap: 10MB used / 68MB total** after sync + scrolling through the whole library — the
  virtualizer is genuinely only keeping the visible window's worth of DOM/decoded images
  resident, not accumulating memory as more of the library is scrolled through. This is the
  result that most directly answers "does it stay snappy": no, it does not degrade with more
  data sitting in IndexedDB, because the render path never touches the off-screen 2,020+ photos.
- **Scroll frame timing: avg 125ms, worst frame 438ms**, measured across 21 large discrete
  jumps spanning the full ~59,995px scroll height. This number needs an honest caveat: the test
  jumped to 21 far-apart scroll positions and measured the frame immediately after each jump —
  that's close to a worst-case for a virtualizer (forces a full new render batch of un-decoded
  images every time, unlike continuous scrolling where only a few new rows enter the viewport
  per frame). It is **not** a measurement of smooth native scroll performance, which is what a
  real user actually does. Left as an open question rather than a pass/fail: worth a proper
  continuous-scroll FPS measurement (e.g. via `page.mouse.wheel()` in a loop, sampling
  `requestAnimationFrame` deltas) before concluding whether this needs optimization
  (candidates if it does: lower `overscan`, smaller `estimateSize`, or deferring thumbnail
  `<img>` decode priority).
- Zero console errors throughout the full sync + scroll sequence.

**Not yet tested**: true offline behavior at this scale (the user noted testing offline isn't
meaningful yet since client and server are on the same machine — this needs a real network
deployment, i.e. after moving to NAS, not before).

## Overnight session: geo-tagged moment naming, resource cost

User asked to see auto-geotag moment naming working and its resource cost before continuing.
Confirmed via source research first: PhotoView exposes raw GPS
(`MediaEXIF.coordinates.{latitude,longitude}`) but does **no reverse geocoding itself** — no
Nominatim/Mapbox-geocoding/place-name code anywhere in its scanner or schema (Mapbox is only
used for map *tile rendering* in its own "Places" page, via `myMediaGeoJson`). So this needed
building from scratch, adapted from dad's `initial_script.py`.

**Decision** (written up in full at `docs/decisions/geo-reverse-geocoding.md`, since it's a
real tradeoff worth documenting rather than silently picking): kept Nominatim (OpenStreetMap),
same as dad's script, over a paid commercial geocoding API (rejected — external paid
dependency cuts against the project's own "avoid dependence on services that might disappear"
principle) or an offline dataset (rejected — coarser results, and the 1 req/sec rate limit
turns out not to matter once you account for caching, see below).

**Built**: `server/geocoding.py` — `suggest_location_label(lat, lon)` and
`suggest_moment_label(photo_gps_list)`, adapted from dad's `get_location_details`/`_slug` as a
standalone reusable module rather than inline logic. Same coordinate-rounding cache dad's
script used (3 decimal places ≈ 110m). Designed as a *suggestion* only — never overwrites an
existing moment label, matching the "auto geo name, user can override" idea from the original
design conversation.

**Resource cost, measured directly** (plain `uv run` Python process, not inside PhotoView's
container — this is intentionally a separate, lightweight step, not something added to
PhotoView's own scan pipeline):
- **~1.1 seconds per fresh (uncached) lookup** — entirely the mandatory Nominatim rate-limit
  sleep, not real compute. A batch of 10 new moments in one scan takes ~11 seconds.
- **~0.004ms per cached (repeat) lookup** — effectively free once a coordinate's been looked up
  once, confirming the caching design removes the rate limit as a practical concern for
  anything except the very first time a new location is seen.
- **Peak RSS: ~40MB.** Negligible against the 1GB DS124 budget — smaller than PhotoView's own
  idle footprint (~107MB, see earlier memory section).
- **Real results verified, not synthetic**: tested against 10 real-world coordinates spanning
  London, Paris, Madrid, Berlin, Edinburgh, Rome, Amsterdam, Stockholm, Prague, Vienna — all
  returned sensible `street.city.country` labels (e.g. `piazza-della-repubblica.rome.it`).

**Not yet wired into the moment-creation flow** (`reorganize.py`'s `rename_moment`/
`merge_moments`/`move_photos` don't call this yet) — next step is deciding exactly when a
suggestion should be generated (at moment-creation time only? offered as a one-click "suggest
a name" action in the UI, rather than automatic?) — leaning toward the latter, since it keeps
network calls opt-in rather than happening silently during every reorganize operation, but
this is a UX-level call worth confirming rather than assuming.

## Face detection resource cost — measured directly, and it's a real problem

User asked to see face detection's actual resource cost (echoing the earlier estimate from
issue #740 that flagged it as risky, but that was never measured directly on this project's
own hardware/library). Re-enabled it (`PHOTOVIEW_DISABLE_FACE_RECOGNITION: "0"`), fully wiped
PhotoView's DB, and ran a fresh scan of the full ~2,097-photo real-photo library (same set
used in the earlier no-face-detection scale test, so the comparison is apples-to-apples).

**Result: idle steady-state after the scan settled at ~722MB RSS (VmRSS) / ~830MB as reported
by `docker stats`** — **72-83% of the DS124's entire 1GB budget**, for a task that isn't even
DSM's own overhead, just PhotoView. Contrast directly with the equivalent no-face-detection
scan of the same library: idle steady-state settled at **~125MB** (see the earlier "Scale
test: ~2,100 real, full-size photos" section). **Face detection alone accounts for
roughly 6x the resting memory footprint** once a library has been scanned.

The scan itself also took noticeably longer and stayed CPU-hot for extended stretches (visible
via `docker stats`' CPU% oscillating rather than settling), consistent with face detection
being CPU-bound per-photo (confirmed earlier via source research - `go-face`/dlib, no GPU
support) on top of the existing thumbnail/blurhash work.

**Conclusion: leave `PHOTOVIEW_DISABLE_FACE_RECOGNITION: "1"` as the default** (reverted after
this test) - this isn't a marginal call, 72-83% of a 1GB device's total budget for one optional
feature is not something to enable on the real DS124 without a lot more headroom to spare, and
this project doesn't have a use for face grouping/tagging that justifies that cost right now.
The one caveat: this Mac's darktable/dlib build may perform differently than the DS124's ARM
build (RTD1619B) - the *relative* 6x cost multiplier is likely to hold regardless of absolute
hardware, but the absolute numbers should be re-verified on real hardware (already flagged as
the outstanding next-step #6) before treating this as fully final.

## Overnight session summary

Worked autonomously through the user's full list overnight, committing at each real milestone
(see git log) and writing a `docs/decisions/*.md` file for every design choice that had real
tradeoffs, rather than blocking on questions. All items from the original list were addressed:

- **Geo-tag moment naming**: built (`server/geocoding.py`, opt-in "Suggest name" button),
  measured (~1.1s/fresh lookup, ~40MB RSS, negligible), tested against real GPS-tagged photos.
- **Face detection resource cost**: measured directly (~72-83% of the DS124's 1GB budget,
  ~6x the no-face-detection cost) - confirmed it stays disabled by default.
- **Range-select → new moment**: tap-to-select UI + `/moments/create` endpoint, tested
  end-to-end (3 photos selected across moments → new moment created correctly).
- **Full-screen viewer**: thumbnail-first → full-res swap, bounded LRU full-res cache,
  server/local source badge, left/right nav, all tested working.
- **Download/share to device**: three-tier fallback (share sheet → mirrored folder save → plain
  download), two real bugs found and fixed by testing (detached anchor, `AbortError` swallowed
  as false success).
- **Metadata panel**: researched PhotoView's actual EXIF schema first, built with what's free,
  documented the ~20% of fields that are genuinely missing.
- **QuickConnect**: researched and ruled out - architecturally incapable of reaching custom
  Docker services, not a security tradeoff to weigh.
- **Year/landmark scrubber**: right-edge rail, jump-to-year via `scrollToIndex`, landmark
  labels from top-photo-count moments per year.
- **Geo-based moment testing**: confirmed location deliberately doesn't factor into grouping
  (only naming) - this was already correct, documented why.
- **Light Picasa-adjacent UI + left panel + auto-sync**: removed unused Vite scaffold CSS,
  added a small CSS-custom-property theme, restructured to a left sidebar, sync now fires
  automatically on login.
- **Adjustable grid density**: 3-20 photos/row slider, CSS Grid layout, localStorage-persisted.

**Real bugs found by testing, not review** (worth noting as a pattern, not just a list):
pagination silently truncating at 500 photos, FastAPI's cookie alias mismatch, a Dexie
migration that silently excluded pre-existing data from a new index, a detached-anchor
download that silently did nothing, and an `AbortError` incorrectly treated as success. Every
one of these looked correct on read-through and only surfaced by actually running the app.

**What's still open, for a future session**:
- Real DS124 hardware validation (next-step #6) - everything measured tonight was on this Mac.
- Continuous-scroll FPS measurement (next-step #9) - the discrete-jump scroll test result was
  inconclusive, flagged rather than treated as pass/fail.
- ~~IndexedDB still keyed by PhotoView's `id`, not content hash~~ - fixed, see "Stable per-photo
  id (photo_id)" section below (keyed by a server-minted UUID, not content hash or PhotoView's
  id - content hash was considered and rejected, see that section for why).
- ~~No merge-moments UI~~ - fixed, see "Merge-moments UI + login session persistence" below.
- ~~No session persistence for login~~ - fixed, see the same section.
- The visual design pass touched the most-visible chrome (sidebar, moment headers, buttons)
  but didn't do a full pass over every inline style in `Timeline.tsx`/`PhotoViewer.tsx` -
  deliberately scoped conservatively this late in a long session to avoid destabilizing
  already-tested interactions; a follow-up pass could go further.

## Post-overnight bugfix round

User feedback after the overnight session (verbatim, lightly excerpted): grid density slider
producing rectangles instead of shrinking squares, no moment title shown in the full-screen
viewer, wanted a real continuous scrubber track with a moving indicator (not the static
year-label list), and a `/moments/create` 500 Internal Server Error when creating a moment
from selected photos. Four fixes, all independently verified via Playwright before committing
together.

**1. `/moments/create` 500 error.** Root cause: `reorganize.move_photos` called
`shutil.move()` with no existence check first. The reported crash's photo paths were stale -
PhotoView's DB (and the client's IndexedDB cache) can legitimately lag behind the real
filesystem, and in this case the exact photos had already been moved into a different moment
during earlier interactive testing. An unguarded move on a path that no longer exists throws
an uncaught `FileNotFoundError`, which FastAPI turns into an opaque 500 with no useful message
- and worse, if it happens partway through a multi-photo batch, some photos have already moved
and others haven't, with no record of which. Fixed by validating every input path up front
(`missing = [f for f in photo_paths if not f.is_file()]`) in all three of
`move_photos`/`rename_moment`/`merge_moments`, raising `FileNotFoundError`/`FileExistsError`
before any filesystem mutation begins, plus a shared `_run_reorganize()` helper in `api.py`
that catches those and returns a clean 409 instead. Verified by direct curl reproduction of
both the original crash (captured from a debug log) and the fixed behavior (clean 409 on stale
data, 200 with correct output on a genuine create).

**Side-effect bug found during the above investigation**: `create_moment`'s date-prefix logic
used `min(host_paths, key=lambda p: p.name)` - sorting by the photo's own *filename*
(`IMG_1007.JPG`), which has no chronological meaning across different moments. The intent
("use the earliest photo's moment date as the new folder's date prefix") needs sorting by each
photo's *parent folder name* instead, since that's the `YYYY-MM-DD`-prefixed, chronologically
sortable value. Fixed to `min((p.parent for p in host_paths), key=lambda d: d.name)`.

**2. Square tiles at every grid density.** The grid previously used a fixed `ROW_HEIGHT`
independent of the actual computed column width, so tiles stretched into rectangles as
`columns` changed (width shrinks with more columns, height didn't). Replaced with a `tileSize`
derived from the real container width (`(containerWidth - GAP*(columns-1)) / columns`) tracked
via `ResizeObserver`, and switched the row container from flexbox to CSS Grid so width is
grid-driven and height now matches it.

This didn't work on the first attempt: `containerWidth` stayed stuck at its `0` fallback no
matter what, confirmed by temporarily logging every render. Root cause (see the new CLAUDE.md
gotcha): the `ResizeObserver`'s setup was a `useEffect(() => {...}, [])` reading a `useRef` -
that effect fires exactly once, at initial mount, and the component's first render(s) hit an
`if (!photos) return <div>Loading…</div>` early return (Dexie's `useLiveQuery` hadn't resolved
yet) whose JSX has no ref at all. So the effect ran once against a still-`null` ref, bailed via
its own `if (!el) return` guard, and never got a second chance once the real grid div (with the
actual `ref={parentRef}`) mounted on a later render. Fixed by switching to a **callback ref** -
a plain function passed as `ref={parentRef}` that calls `setScrollElement(node)` whenever React
actually attaches (or detaches) the node - which correctly fires on whichever render that
turns out to be. This also required updating TanStack Virtual's `getScrollElement` (previously
`() => parentRef.current`) to read the `scrollElement` state instead, since a callback ref is
a plain function with no `.current` to read.

Verified via Playwright at columns=3/6/12: all three produced exact 1:1 width:height ratios,
and tile size correctly shrank as column count increased (173px @ 6 cols -> 85px @ 12 cols).

**3. Viewer moment title.** Added a centered, top-of-screen label
(`data-testid="viewer-moment-title"`) showing `photo.moment` in `PhotoViewer.tsx`, styled to
sit above the image without blocking it. Verified present and populated via Playwright.

**4. Scrubber v2 - continuous track + moving indicator.** Full rewrite of `YearScrubber.tsx`,
design rationale in `docs/decisions/timeline-scrubber-v2.md`: replaced the v1 static
year-label list with a vertical track spanning the viewport, year tick marks positioned
proportionally by row index, a moving circular indicator synced live to the grid's actual
scroll position (via a `scroll` listener on `scrollElement`, converting `scrollTop /
(scrollHeight - clientHeight)` to a fraction), and click/drag-anywhere-on-the-track to jump
(`onPointerDown`/`onPointerMove` converting the pointer's Y position to a fraction, passed up
to call `virtualizer.scrollToOffset(fraction * totalSize)`). Verified via Playwright:
indicator's `top` position changed after programmatically scrolling the grid (`calc(0% + 8px)`
-> `calc(64.25% + 8px)`), matching the scroll fraction.

**Still deferred** (explicitly, by the user's own phrasing - "when we add the rest of the
photos"): testing against the full/larger photo library, and re-testing geotag naming at that
scale. Not started this round - current testing remained against the small test library.

## Follow-up: apparent photo "duplication" after create-moment

User report after re-testing the above fixes: creating a new moment worked, but some photos
appeared duplicated (didn't seem to leave the original folder), plus a second attempt hit
`409: 2 photo(s) no longer exist on disk`.

Investigated both. The 409 was the new validation working as intended, not a bug: the client's
selection was against IndexedDB rows for photos that a *previous* create-moment call had
already moved, so the stale paths correctly failed instead of crashing.

The "duplication" was real but not on disk or in appdb - both were checked directly (`find` for
duplicate filenames, `sqlite3` for duplicate content-hash rows) and both were clean; the two
sample photos in the report existed in exactly one place each. The duplication was in the
**client's rendered view**: `createMoment`/`MomentHeader`'s rename only set a "re-sync to see
it" status message rather than syncing automatically. Because PhotoView reissues a brand new
`id` for any moved file (invariant #1), the old-id IndexedDB row for a just-moved photo isn't
pruned until the *next* sync runs - so between a create/rename and the next manual sync, the
photo visually persisted at its old location (rendered from the stale row) while also having
already moved on disk, reading as "it got duplicated / didn't leave."

Fixed by wiring an `onNeedsSync` callback from `App.tsx`'s existing `runSync` down through
`Timeline`, called immediately after a successful `createMoment` or moment rename instead of
just showing a status message. Verified via Playwright: read IndexedDB directly (`indexedDB.open`
+ `getAll`, since the built preview bundle has no importable `db.ts` module to reach from a
test script) before and after a create-moment action - after the auto-triggered sync
completed, total photo count was unchanged (2 moved out of an old moment, 2 into the new one,
net zero), all ids and paths were unique, and the new moment's 2 photos were both present.

**This turned out to only narrow the window, not fix the actual bug** - the user reported
still seeing stale duplicates after a manual reload and re-sync in their real browser, which a
fresh Playwright profile didn't reproduce (clean data there), pointing at accumulated stale
IndexedDB state from before the auto-sync fix landed, combined with the diff/prune logic in
`sync.ts` not reliably converging even given a chance to run. Root cause, once actually
examined rather than assumed fixed: the client's IndexedDB was (still) keyed by PhotoView's
own `id` - the auto-sync fix made syncs happen sooner, but every sync was still keying on a
value that churns on every move, so nothing guaranteed the stale old-id row would actually get
pruned rather than racing/lingering.

## Stable per-photo id (photo_id), replacing PhotoView's id client-side

Full design writeup: `docs/decisions/stable-photo-id.md`. Summary of the reasoning and what
changed:

- **Content hash was considered and rejected as the client's key.** It's what `server/appdb`
  already uses, so reusing it looked obvious - but an in-place edit (any tool, not just this
  app, rewriting a photo's original bytes - e.g. a phone's own camera-roll editor) changes the
  hash, which would silently orphan that photo's favorite/moment history on the next sync.
  Also: hashing is a full read of the file's bytes - appdb only ever computes it lazily
  (photos touched by a write path), and re-hashing the entire ~28,000-photo/user target
  library on every sync would not fit the DS124's constraints.
- **Chosen: a server-minted UUID (`photo_id`), decoupled from both path and content.** Minted
  once, the first time a path is seen, kept forever after regardless of moves or edits.
  Survives moves/renames/merges exactly (invariant #3 - the app is the only thing that ever
  moves a file, so every reorganize call already knows old_path -> new_path precisely, no
  inode-matching or heuristic needed). Survives in-place edits (identity doesn't depend on file
  bytes at all).
- `appdb/schema.sql` and `db.py`: added `photo_id TEXT` to `photos`, migrated in via
  `ALTER TABLE` (guarded by a `PRAGMA table_info` check, safe on every `connect()`) rather than
  relying on `CREATE TABLE IF NOT EXISTS`, which doesn't add columns to an already-existing
  table. `upsert_photo` mints a UUID only on first insert and deliberately excludes `photo_id`
  from its `ON CONFLICT ... DO UPDATE SET` clause, so a later upsert of the same content_hash
  (e.g. from a move) keeps the existing id rather than generating a new one.
- New `reorganize.ensure_all_photos_registered()`: walks `PHOTOS_ROOT` for image files and
  registers (hashes + upserts) any path not already known to appdb - needed because appdb was
  otherwise populated lazily and most of the library would have no `photo_id` at all. Skips
  any path already present, so a repeat call over an already-registered library does zero
  hashing, only one query per photo.
- New `GET /photo-ids` endpoint in `api.py`: calls `ensure_all_photos_registered()`, returns
  the full `path -> photo_id` map in PhotoView's container-relative path form (matching what
  the client already gets from `myTimeline`'s `path` field), so the client never needs to see
  appdb's host-absolute paths.
- `sync.ts`: fetches `/photo-ids` alongside the timeline, resolves each media item's stable id
  by path, and diffs/upserts/prunes IndexedDB using that id instead of `media.id`. A media item
  whose path isn't yet in the map (a race with a reorganize call still mid-registration) is
  skipped for that sync pass rather than cached under a wrong id.
- `db.ts` bumped to schema v4: `.upgrade()` clears the `photos` table outright rather than
  attempting a row-by-row id remap (old rows have no valid mapping to the new scheme) - a
  one-time full resync from empty is cheap and matches the project's own principle that the
  thumbnail cache is disposable, not a source of truth.

**Verified**: direct curl test confirmed a photo's `photo_id` is identical before and after a
`rename_moment` call (same UUID, new path). Playwright end-to-end: after the v4 migration, a
freshly-synced photo's `id` is a real UUID (not a small PhotoView-style integer); a
create-moment action followed by auto-sync produced zero duplicate ids/paths; a full page
reload + re-sync (reproducing the user's exact report) also produced zero duplicate paths.

**Kept as designed, considered and rejected**: switching `stars.json`'s export key from
`content_hash` to `photo_id` was raised and declined - that file's whole purpose is to survive
even if appdb/this app disappear entirely ("readable by anyone with a text editor in 20
years"), and `photo_id` is meaningless without appdb to look it up against, whereas
`content_hash` is independently recomputable from the raw file bytes by anyone, no database
required. `photo_id` is for live sync identity (must survive moves, doesn't need to survive
the app's own extinction); `content_hash` stays for `stars.json`'s durability role (the
reverse tradeoff).

## Follow-up: newly-created moments not appearing after sync (silent GET caching)

User re-tested the photo_id fix above: duplicates were gone after a refresh, but a new problem
surfaced - creating a moment ("lousy", then "test3", then "test4") reported success server-side
("Created ... with N photo(s)"), but the new moment never appeared in the app after syncing,
with no console errors. Reproduced in both Safari and Chrome, ruling out a browser-specific
IndexedDB quirk (the first suspicion, given Safari's known IndexedDB reliability issues) -
same failure in both, so it had to be something shared.

Investigation: `appdb`, `reorganize.ensure_all_photos_registered()`, PhotoView's own
`myTimeline`, and `server/api.py`'s `/photo-ids` endpoint were all directly verified correct
after each of the user's repro attempts - the new moment's photos and their `photo_id`s were
present everywhere on the server side, every time. The gap was specifically "does the client's
*next* sync in the same session pick up the change" - the very first sync in a session was
always correct (matches the user's own report: "initial open and sync had lousy moments").

Root cause: `client/web/src/api.ts`'s `get()` helper (added for `/photo-ids`, see above) called
a plain `fetch()` with no `cache` option. `/photo-ids` is a GET whose response changes on every
reorganize call, but nothing marked it as such - FastAPI sets no `Cache-Control` header by
default, so an identical-looking subsequent GET could be served from the browser's own HTTP
cache, silently, no error surfaced anywhere (this is heuristic browser caching, not something
either side's code explicitly requested - which is exactly why it produced "no errors, still
wrong data" rather than a visible failure). PhotoView's own timeline fetch (via `graphql-request`,
a POST) isn't subject to the same heuristic caching, which is why that half of every sync
always looked correct while `/photo-ids` didn't - explains why the *first* sync in a session
(nothing cached yet) was always right and later ones weren't.

Fixed on both sides, deliberately redundant: `api.ts`'s `get()` now passes `cache: 'no-store'`
on the fetch call, and `server/api.py`'s `/photo-ids` handler sets an explicit
`Cache-Control: no-store` response header. Either alone would likely have been enough, but
relying on only the client side leaves any other caller (or a future service-worker route)
exposed to the same silent staleness, and relying on only the server side depends on every
client respecting response cache headers exactly - not worth the risk for something the user
already hit twice.

Verified via Playwright: several manual syncs first (to warm the browser's cache under the old
code's conditions), then a create-moment action, confirmed the new moment's photos land in
IndexedDB and render immediately after the *auto-triggered* sync with no manual reload needed
- run repeatedly (5x) to rule out a lucky pass rather than a real fix, matching the user's
"i tried again with another selection, same issue" pattern of hitting it repeatedly before.

A separate, not-yet-investigated issue surfaced during this debugging and noted for later: two
`GET /api/photo/<name>.JPG` requests returned `502 Bad Gateway` in the user's browser
(full-screen view of specific photos) - not reproduced in this session's own testing, flagged
rather than chased down since it wasn't blocking the photo_id/caching investigation and could
be transient (e.g. PhotoView regenerating a full-res proxy image).

## Follow-up #2: the no-store fix wasn't the whole story - sync.ts's own diff logic was broken

The no-store fix above was real and necessary, but the user re-tested (cleared cache, hard
reload, disabled browser cache in dev tools) and **still** hit "create moment" and, newly,
"rename moment" doing nothing visible - ruling out caching definitively, since browser caching
was now impossible by the user's own test conditions.

Traced with a network-order-logging Playwright script (request/response timestamps for
rename/create/photo-ids/graphql) rather than guessing again: the server-side call order was
always correct (mutation completes, then a fresh photo-ids + timeline fetch, both already
reflecting the new state) - so the bug had to be in what `sync.ts` did with correct data it
already had in hand.

Found it on inspection of the diff logic itself: `toFetch = remoteWithIds.filter(r =>
!cachedIds.has(r.id) && ...)`. This was fine under the *old* PhotoView-id scheme, where a
moved/renamed photo always got a **new** id (so it always looked "new" and got refetched with
current metadata) - but that's exactly the property the photo_id fix (previous section)
deliberately removed. With a stable id, a renamed/moved photo's id is *always* already in
`cachedIds`, so it was permanently excluded from `toFetch` and its stale `moment`/`path` fields
in IndexedDB were never touched again. The photo_id fix solved the duplication bug perfectly
but broke propagating rename/move metadata changes at all - two different bugs that happened to
look similar ("the UI doesn't reflect what I just did"), the second only exposed once the first
was fixed.

Fixed in `sync.ts`: split the diff into two passes. `toFetch` (id not cached at all) downloads
a fresh thumbnail blob, as before. A new `toUpdate` pass (id cached, but `path`/`moment`/
`momentPath`/`favorite` differ from the cached row) applies a cheap `db.photos.update()` with
no blob re-download. `App.tsx`'s sync status message now reports `updated` alongside
`added`/`removed`/`total` so this path is visible, not silent.

Verified via Playwright, both directions:
- **Rename**: renamed a moment, waited for the auto-triggered sync, confirmed 0 photos remained
  under the old moment name and all were present under the new one, with a rendered header.
- **Create**: selected 2 photos, created a new moment, confirmed both appear under the new
  moment with a rendered header.
Both run repeatedly (4x) to confirm reliability, not a lucky pass - matching the user's own
"I tried again with another selection, same issue" pattern of hitting this multiple times
before it was actually fixed.

**Side fix found in the same pass**: `rename_moment`'s `new_path = moment_path.parent /
new_label` used the raw label with no date-prefix handling - the rename UI's input is
pre-filled with the full folder name so the normal edit-in-place flow keeps the prefix, but
clearing the field entirely (easy to do, confirmed via one of this round's own test runs)
silently produced a prefix-less folder name, breaking the chronological sort every other part
of the system depends on (moment ordering, `merge_moments`' "earliest date wins",
`create_moment`'s own date-prefix derivation). Added `_with_date_prefix()`: a `new_label`
without its own `YYYY-MM-DD-` prefix inherits the moment's existing one; a label that already
has one is passed through untouched (no double-prefixing). Verified via curl both ways.

**Also fixed**: the PWA manifest referenced `pwa-192x192.png`/`pwa-512x512.png` that never
existed in `client/web/public/` (only SVGs did) - surfaced as a real (if cosmetic) console
error the user hit on every sync ("Error while trying to use the following icon from the
Manifest"). Generated both PNGs (simple mountain/sun photo-app icon, not the default Vite
logo) plus an `apple-touch-icon.png`; rebuilt and confirmed both load 200 and the service
worker's precache list grew from 7 to 12 entries.

## PhotoView user cleanup: admin should not be the daily-use account

Side effect of debugging the scrubber against real user accounts: while probing PhotoView's
`updateUser` mutation to find where the scale-test library's `alice`-named folder actually
lived, an id guess (`id: "2"`) accidentally overwrote `bob`'s password, and a follow-up probe
of `id: "1"` overwrote `admin`'s own password too - both immediately restored/reset once
noticed (see git history / this session's own correction). In the course of this, discovered
that **`alice` was never a real PhotoView user** - only a folder name that happened to sit
under `admin`'s own root path (`/photos/alice`) from early interactive testing. Only two real
accounts exist: `admin` (id 1, PhotoView's root path `/photos/alice` - a naming
mismatch from early setup, not corrected here) and `bob` (id 2, root path `/photos/bob`).

Per explicit user request ("admin credentials should never really be used... put scaletest
photos into user... keep the app like it should be used"): set `admin`/`admin` and `bob`/`bob`
as the working credentials, and moved the entire 2000-photo scale-test library from its
incorrect location under `admin`'s `alice`-named folder into `bob`'s actual root
(`server/data/photos/bob/<year>/scaletest-<month>/`), then triggered a fresh `scanAll` to
reconcile. **`bob`/`bob` is now the account meant for daily app use** - matches the real design
intent (an actual family member's account, not the system admin account) - `admin`/`admin`
still works but should be reserved for admin-only operations (`scanAll`, user management) going
forward, mirroring how `server/api.py`'s existing `admin_token`/`user_token` split already
treats these as different-purpose credentials server-side.

## Scale test #2: 2062 real full-size photos (62 original + 2000 phone-backup photos)

Follow-up to the earlier ~2,097-photo scale test (see "Face detection resource cost" section) -
this run used `server/fakelib/generate_scale_test_library.py --count 2000` against a real phone
camera-roll backup (9.02 GB, real EXIF, real file sizes - not the small synthetic samples the
main fake-library generator uses), giving a genuine high-volume, high-filesize scan rather than
a repeat of the earlier count-only test.

**Scan resource usage** (`docker stats`, sampled every 15-18s throughout): memory climbed
gradually and smoothly from ~130MB (idle) to a peak of ~366MB during active thumbnail
generation, settling around 317-330MB once the queue drained - comfortably within the DS124's
1GB budget, with face recognition already disabled (`PHOTOVIEW_DISABLE_FACE_RECOGNITION=1`, see
the existing gotcha in CLAUDE.md) the whole time. CPU alternated between near-0% (waiting on
I/O over the network-mounted source volume) and 100-120% (active thumbnail/blurhash generation)
in bursts, consistent with a single-core-bound worker doing real image processing rather than a
runaway or leaking process. Total scan wall-clock time was on the order of 10-15 minutes for
2000 full-size real photos - acceptable for a one-time/occasional operation, not something that
needs to be fast on every app open (matches the existing "sync via full re-list + diff" design,
which only re-downloads what's actually new).

**Idle memory after the scan, corrected**: the user directly checked memory well after the scan
finished and CPU had dropped to 0% - **380MB idle** per `docker stats`, not back down to the
~130MB baseline seen at the smaller (~97-photo) library size. This contradicted an earlier
(wrong) claim in this same section that memory "scales with active processing load, not library
size at rest." Investigated properly rather than left as a surface-level `docker stats` number
(see next section) - the real picture is more nuanced than either the original or the corrected
claim above.

### Deep dive: what's actually in that memory, and is it a real problem

`docker stats`' 380MB figure itself undersold the real number - checking the container's cgroup
directly (`docker exec ... cat /sys/fs/cgroup/memory.current`) showed **~1012MB, 94.3% of the
1GB `mem_limit`** at the same moment. Breaking that down via `memory.stat`:

- **`anon` ~249MB** - real application memory: the Go process itself (`/proc/1/status` VmRSS
  confirmed ~220MB) plus the persistent `exiftool -stay_open` worker process (~36MB).
- **`file` ~607MB** - this is the actual driver, and it is **not application memory at all**:
  Linux page cache from reading the ~8.6GB of full-size source photos during the scan
  (`du -sh /photos` inside the container confirmed the full mount is 8.6GB) - the kernel
  opportunistically keeps recently-read file bytes cached in free RAM because that's normally
  free performance, not because anything asked it to.
- `memory.events` showed `max: 16651` (the cgroup hit its ceiling and got reclaimed against
  16,651 times) but **`oom_kill: 0`** - proof the reclaim mechanism is working as designed
  (evicting reclaimable page cache under pressure), not that anything is leaking or crashing.

**Research into PhotoView-side mitigations** (a background agent checked PhotoView's actual Go
source, official env var docs, and GitHub issues): confirmed there is **no PhotoView env var or
setting to bound this** - it doesn't call `posix_fadvise`/`madvise` to hint the kernel to drop
cache after reading a source file (checked the scanner/media Go source directly, no hits), and
the official env var list has only 3 feature-disable flags (face recognition - already off here
-, video encoding, RAW processing) plus DB/serving config, nothing for buffer/cache sizing.
The only concurrency lever, `ConcurrentWorkers` (a DB-stored admin setting, not an env var,
resolved via `siteInfo.concurrentWorkers` / `ChangeScannerConcurrentWorkers`), was already at
its SQLite-driver default of 1 (confirmed via GraphQL query) - already at the minimum, nothing
left to reduce there. No matching GitHub issues found on this exact page-cache-vs-cgroup
phenomenon upstream, either - it doesn't appear to be a commonly-hit/discussed problem for
low-RAM PhotoView deployments.

**Attempted mitigation, blocked by this being a Mac, not real hardware**: cgroup v2 supports a
*soft* limit (`memory.high`) that triggers earlier, gentler reclaim before the hard
`memory.max` ceiling is hit - in principle a real lever (Docker's `mem_reservation` flag is
supposed to map to it). Tested directly (`docker update --memory-reservation 400m`) and
confirmed it **did not actually set `memory.high`** in this environment (`memory.high` stayed
`max` before and after) - this Docker setup is `Docker Desktop` on macOS with cgroup driver
`cgroupfs`/v2, which adds a Linux-VM virtualization layer on top of the host OS; the flag isn't
wired through the same way it would be on a real Linux host. **This specific mitigation
(setting `memory.high` in `server/docker-compose.yml`) needs to be tested on the real DS124**,
where Docker runs natively on Linux, not inside a VM - deferred rather than chased further on
this Mac, since nothing further can be conclusively verified here.

**User's actual concern, clarified by discussion**: not "is 94% of 1GB alarming as a raw
number" but "will this stay a well-behaved background process that leaves the DS124 free to do
other things" - and the direction requested is explicit: **the server should not try to cache
aggressively for speed; the client already does that (the whole point of the local
thumbnail-cache design) - server-side should stay as lean as possible.** Current assessment
against that goal: PhotoView's own real footprint (~220-250MB) is already small and separate
from the page-cache figure; the page-cache portion is reclaimable-on-demand by the kernel (not
memory PhotoView is holding hostage) and this session's own testing already demonstrated
successful reclaim (16,651 events, zero OOM kills) under real pressure. **Deferred, not
resolved**: set `memory.high` (a soft ceiling below `mem_limit`, e.g. `600m` if the real 1GB
`mem_limit` stays) in `server/docker-compose.yml` once real DS124/DSM testing begins, so page
cache gets trimmed earlier and more gently rather than right at the hard ceiling - this is the
concrete next step, verifiable on real hardware even though this Mac's Docker setup couldn't
prove it out.

**Client-side**: initial full sync of all 2062 photos (thumbnail download + IndexedDB write)
completed without error via the app's own sync button, exercised as part of verifying the v3
scrubber (below) - the client had no trouble with the 20x larger dataset either.

## Scrubber v3: fisheye label density, solid track, arrow indicator

User feedback on the v2 scrubber, tested for the first time against a real ~2000-photo
library: wanted it actually draggable (v2's handlers were structurally present but the hit
target - a 2px line and small dot - was too easy to miss), moved off the viewport edge, a
solid-color track instead of a thin line, the position indicator changed from a gold dot to a
`>` arrow in Flexoki red (`#af3029`, distinct from the gold already used for favorites), and -
the more substantial ask - non-linear label density: more detail (individual moment names)
near the current scroll position, larger-moments-only in a middle band, years-only further
away. Full design writeup: `docs/decisions/timeline-scrubber-v3.md`.

**Design decision**: "current position" for the fisheye effect is the live scroll position
(recomputed on every scroll tick), not a fixed anchor like "today" - so scrolling deep into
2019 brings 2019's moment names into full detail rather than keeping the top of the timeline
permanently over-detailed. `computeYearMarkers` extended to return every moment (not just
top-2/year, which v2 used) with each moment's photo count, so the fisheye filtering has the
full candidate list to work with at render time.

**Bug found via first real-scale test, not code review**: the initial fisheye implementation
(distance-based inclusion + opacity, no collision handling) looked correct in isolation but
produced a genuinely illegible result against the real 2000-photo library - multiple
same-day-ish moments near the current scroll position overlapped into an unreadable stack of
folder names, confirmed via an actual Playwright screenshot (not just reasoning about the
code). Fixed by adding a proximity-sorted greedy collision-avoidance pass on top of the
distance/opacity logic: candidates are sorted closest-to-current-position first, and any
candidate within `MIN_LABEL_GAP_PX` (16px) of an already-placed label (measured in the track's
actual rendered pixel height, tracked via `ResizeObserver`) is dropped rather than shown
overlapping. Re-verified via a second screenshot: label count near the current position dropped
from an illegible ~19 to a clean, spaced 7.

**Also fixed via the same screenshot-driven pass**: the initial track color
(`--scrubber-track: #e2e2e2`) was too close to the page background (`--bg: #fafafa`) to read as
a "solid" track at all in the actual screenshot, despite being visually reasonable when read as
a color value - darkened to `#d0d0d0` with a subtle inset border, and widened from 6px to 8px.
The arrow indicator was similarly enlarged (7px/9px triangle -> 10px/14px) after the first
screenshot showed it as barely perceptible at the original size.

**Verified via Playwright against the real 2062-photo library** (not the earlier small test
set - deliberately, since fisheye density and collision behavior are only meaningfully testable
at real scale): scrubber renders 16px off the viewport edge; drag moves the main grid's
`scrollTop` correctly (confirmed via before/after values, not just that a handler fired);
click-to-jump works (tested at both extremes of the track, confirmed via `scrollTop` moving to
the expected end); indicator color resolves to the correct Flexoki red
(`rgb(175, 48, 41)` = `#af3029`); label count and spacing confirmed via two before/after
screenshots (overlapping mess -> clean, non-overlapping set).

**Follow-up bug, found by the user at 15 columns/row**: the photo grid's rightmost column
visibly overlapped the scrubber (a real screenshot at 2556x1424 confirmed this - the last
column's thumbnails ran directly under the scrubber's track). Root cause: the scrubber is
`position: fixed`, so it draws on top of the scroll container's own content rather than
participating in its layout - the grid's own `containerWidth` measurement (via `ResizeObserver`
on the scroll container, used to compute `tileSize`) had no reason to account for space the
scrubber visually occupies. Fixed by adding `paddingRight` (112px - matches the scrubber's
`right: 16` + `width: 88` plus a small margin) directly to the scroll container's own style in
`Timeline.tsx`: since `ResizeObserver`'s `contentRect` excludes padding, this makes
`containerWidth` (and therefore `tileSize`/column width) automatically shrink to leave the
reserved space, with no separate calculation needed. Verified via Playwright at 15 columns/row:
computed a positive gap between the rightmost visible thumbnail's right edge and the
scrubber's left edge (no overlap), confirmed visually via a fresh screenshot.

## Merge-moments UI + login session persistence

Two items closed off "what else needs doing" - both real, contained additions to close known
gaps from earlier in this session.

**Merge-moments UI** (`Timeline.tsx`): a "Merge moments" toolbar toggle, mutually exclusive
with the existing photo-level "Select photos" mode (each disables the other's button, avoiding
a confusing dual-selection UI). `MomentHeader` grows a checkbox when merge mode is active
(clicking the header itself also toggles it); once 2+ moments are checked, a label input +
merge button appear, calling the already-existing `mergeMoments` API client function (the
server-side `/moments/merge` endpoint needed no changes - it's been there since an earlier
session). Auto-syncs afterward, same pattern as create/rename.

Verified directly against the real reorganized 2000-photo library: selected two real moments,
merged them under a new label, confirmed the merged folder exists on disk with the combined
photo count and the total library count stayed at exactly 2000 (no loss). Playwright automation
of this specific flow turned out unreliable against a *virtualized* list (only visible moment
headers/checkboxes exist in the DOM at any time, so blindly indexing `.nth(1)` without
accounting for what's actually scrolled into view produced flaky false negatives) - the feature
itself was confirmed working by inspecting real server/filesystem state directly rather than
trusting a fragile test script's timing.

**Login session persistence** (`photoview.ts`/`App.tsx`): the `auth-token` cookie `login()` sets
had no `Max-Age` at all, so it defaulted to a browser*-session* cookie (survives a reload within
the same browser session, but not a full browser restart) - and more importantly, the React app
had no way to *know* a still-valid cookie existed on mount, so `loggedIn` state always started
`false` and forced the login form on every single reload regardless. Fixed with two changes:
the cookie now gets an explicit 30-day `Max-Age` (bounded, not "forever" - PhotoView's tokens
don't appear to expire server-side per this project's own earlier observation, but treating that
as a hard guarantee felt presumptuous); and a new `checkExistingSession()` (a minimal
`myUser { username }` GraphQL query) runs once on app mount, restoring `loggedIn`/`username`
state if the cookie is still accepted, falling through cleanly to the normal login form if not
(an expired/revoked/garbage cookie was tested directly via `context.addCookies` with a fake
token - correctly falls back, doesn't get stuck). A brief "Checking session…" state avoids
flashing the login form for a moment before the check resolves.

Verified via Playwright: fresh browser (no cookie) correctly shows the login form; after
logging in, the cookie carries a real future expiry (not `-1`/session-only); reloading the same
page skips the login form entirely and lands directly on the logged-in view; a manually-injected
invalid cookie correctly falls back to the login form rather than hanging on "Checking
session…".
