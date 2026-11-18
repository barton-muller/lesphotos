# Decision: replace PhotoView with a first-party indexer + static derivative serving

## Request

"I'm more concerned about speed. Happy to have grey squares that load rather than blurs. But I
want to fundamentally reduce speed [latency] and decrease RAM footprint at idle. How easy is
what we use of PhotoView to replace? What does it do for us? Would it be possible to run a
non-Docker equivalent reducing Docker overhead."

Follow-ups established: videos are in scope (shown as a thumbnail with a play button);
transcoding is explicitly **not** a priority; target scale is ~100GB per user, ~5TB total.

## What PhotoView actually does for us

Exhaustive, from reading `client/web/src/photoview.ts` and `server/reorganize.py`:

1. **Derivative generation** - scans `data/photos/`, writes thumbnail + highres into
   `media-cache/`.
2. **Serving those derivatives** as static files under `/photo/thumbnail/...`.
3. **`myTimeline`** - paginated `{id, title, path, date, favorite, album{title,filePath},
   thumbnail{url,width,height}, highRes{url}}`.
4. **Per-photo EXIF** via `media(id:)`, lazily, for the metadata panel.
5. **Auth** - a cookie token plus a user->root-path scoping model.

That is the entire surface. Critically, it is **not** the source of truth for anything:

- Favorites are authoritative in `appdb` (invariant #2). PhotoView's copy is a mirror we
  maintain purely for the benefit of PhotoView's own web UI.
- Moments are directories on disk. PhotoView contributes nothing.
- Stable identity is `photo_id` from appdb; PhotoView's `id` is explicitly disposable
  (invariant #1).
- Thumbnails are cached client-side in IndexedDB regardless.

So we pay ~125MB idle plus the Docker daemon for thumbnailing, static file serving, and a JOIN
over file metadata.

## The hidden cost: the reconciliation layer

Every rename/merge/move currently does:

1. `scanAll` - a **full-library** rescan (confirmed in `reorganize.py`, not a delta scan)
2. Poll `myAlbums` until the moved paths appear (`_scan_all_and_wait`, up to 180s)
3. For each moved photo, `_find_media_id_by_path` - which fetches the **entire album tree**
   per lookup, in a loop
4. Re-apply `favoriteMedia` for each, to repair the mirror

All of this exists solely because PhotoView's IDs churn on move. We do not have that problem
and would not reintroduce it. At the stated target scale, step 1 alone is disqualifying: a
full rescan of 5TB after renaming one moment is not viable.

**Removing PhotoView deletes this entire layer** - `_scan_all_and_wait`,
`_find_media_id_by_path`, `_reconcile_favorites`, `set_favorite`'s mirror write,
`import_favorites_from_photoview`, and the `admin_token`/`user_token` split that exists only
because `scanAll` is admin-only while `favoriteMedia` is user-scoped.

## Measured baseline

Synthetic dev library (`server/data/photos/`): 2,321 JPEGs, 8.5GB, zero video.
`media-cache/` = 201MB, i.e. **~2.4% of originals**.

Real library (`/Volumes/home/Photos`, SMB mount of the NAS - **the NAS is the DS124 itself**,
i.e. these are the target machine's real contents, not a proxy): 4,443 files, 21.9GB.

| ext | count |
|-----|-------|
| jpg | 3,978 |
| mp4 | 274 |
| mov | 88 |
| heic | 75 |
| jpeg | 26 |
| png/gif | 2 |

Videos: 362 files, 8.7GB, mp4/mov only - no exotic containers.

Video codecs (60-file sample): **~95% HEVC**, remainder H.264. Sampled iPhone `.MOV` carries
`rotation=-90` side data and `pcm_s16le` (uncompressed) audio.

At the 2.4% ratio, a 5TB library implies **~120GB of derivatives**. Fine on a 5TB volume.

## Design

Replace PhotoView with two pieces inside the existing FastAPI service:

**An offline indexer.** Walks the library, and for each file that is new or whose
`(mtime, size)` has changed: registers it in appdb (getting/keeping its `photo_id`), extracts
EXIF, and writes derivatives to `cache/<photo_id>/{thumb,preview}.webp`.

**Static derivative serving.** `cache/` served directly - ideally by a small nginx/Caddy so
image bytes never enter Python. Originals served with HTTP range support.

`server/api.py` gains a `/timeline` endpoint: appdb already holds `photo_id` and path; add
`date`, `width`, `height`, `duration`, `is_video`. It becomes one indexed
`SELECT ... ORDER BY date`, which is strictly cheaper than `myTimeline` - no GraphQL parse,
no join across a foreign schema.

**Auth** replaces PhotoView's cookie/token model with a single shared password issuing a
signed token. This is a family photo server; per-user scoping is not currently used for
anything except PhotoView's own root-path model, which goes away with it.

### Indexer requirements (driven by the 5TB target)

At 8.5GB these would be niceties. At 5TB they are load-bearing, and they are the single most
likely thing to go wrong:

- **Resumable** - killed mid-run (power, reboot, timeout), a restart continues rather than
  starting over. A full initial index of 5TB on a DS124 is plausibly days of wall-clock.
- **Incremental** - `(mtime, size)` check first; never re-hash or re-derive unchanged files.
  Re-runs must be cheap, matching the existing precedent of
  `ensure_all_photos_registered()`.
- **Memory-bounded** - stream one file at a time. Never materialise a full-library list in
  RAM. The DS124 has 1GB total.
- **Interruptible/throttleable** - it must be possible to run this without making the NAS
  unusable for everything else.

### Video

In scope, deliberately minimal:

- **Thumbnail**: one frame extracted at index time via ffmpeg, respecting rotation metadata
  (autorotate on - the sampled iPhone clip is stored landscape with `rotation=-90`; naive
  extraction yields sideways thumbnails). Stored exactly like a photo thumbnail.
- **Grid**: renders as a normal tile with a play-button overlay and duration badge.
- **Playback**: serve the **original** file via HTTP range requests. `<video>` in the viewer
  instead of `<img>`.
- **No transcoding.** Explicitly deferred - see below.

ffmpeg 4.1.9 confirmed present on the NAS (the DS124) (`/bin/ffmpeg`); HEVC decode is
supported in 4.1.x, so frame extraction runs on-device. No off-box indexing step is needed.

### HEIC

75 files today, but every modern iPhone shoots HEIC by default, so this only grows. Pillow
does not decode HEIC without `pillow-heif`. Add the dependency at indexer-build time rather
than discovering it later as a pile of missing thumbnails.

### Client sync at scale

`sync.ts` currently fetches every thumbnail and diffs the whole timeline. ~200MB of
derivatives fits IndexedDB comfortably; ~120GB does not. The sync model needs to become
**windowed** - a recent-N-months resident set, older thumbnails fetched on demand and evicted
LRU (Least Recently Used: when over the size budget, drop whatever was viewed longest ago, on
the bet that recent access predicts future access; a wrong guess costs one re-fetch, not
correctness). `fullres.ts` already implements exactly this pattern for full-resolution images,
so the approach exists in-codebase and does not need inventing.

Note this changes what "offline" means at scale - from "the entire library" to "everything
viewed recently". That is the same tradeoff Google Photos and Immich make, and it does not
apply yet: at the current 21.9GB the derivative set is ~500MB and fits comfortably.

This is not required for the initial replacement (which can ship against the current library
size) but the schema and endpoints should not foreclose it.

## Expected outcome

| | now | after |
|---|---|---|
| processes | PhotoView (Go) + api.py + Docker daemon | api.py + optional static server |
| idle RAM | ~125MB + Docker daemon (~50-100MB) | ~40-60MB |
| rename cost | full-library `scanAll` + N full-tree queries | filesystem move + appdb update |
| container runtime | required | none |

Latency at browse time is expected to be equal or better: both designs serve pre-generated
static files, but the replacement drops a GraphQL layer and a foreign schema join, and the
client already caches thumbnails locally regardless.

## Deferred: HEVC transcoding

**Decision: not now, knowingly.**

~95% of the library's video is HEVC. Browser support is uneven - Safari/iOS plays it,
Chrome/Edge depend on OS-level hardware decode, Firefox effectively does not. Serving
originals therefore means **video playback is effectively Apple-only**. This was accepted as
a deliberate tradeoff ("honestly it's not a priority"); recording it here so it is a known
limitation rather than a future surprise.

Transcoding on a DS124 is not viable in any case - no hardware encoder, weak ARM CPU,
realistically minutes per clip at 100% CPU.

If it ever needs fixing, the intended route is **one-time offline conversion**: convert
HEVC->H.264 on a machine with hardware encode (a Mac, via VideoToolbox, at roughly realtime),
store the H.264 rendition as a derivative alongside the original. This respects invariant #5 -
originals are never mutated; the conversion is a cache artifact. Cost is roughly 1-1.5x the
video bytes, i.e. ~10GB at current volume. It is a batch script, not an architecture change,
which is precisely why deferring it is safe.

Lazy transcode-on-first-play was considered and rejected: the first viewer of any clip would
wait minutes.

## Open questions

None blocking. ffmpeg availability on the target was the last one and is resolved: the NAS is
the DS124 and has ffmpeg 4.1.9, so the indexer runs on-box.

## Status

**Built and validated.** PhotoView is no longer running and nothing in the app depends on it.

### Measured results (2,330-photo library, dev machine)

| | PhotoView | ours | |
|---|---|---|---|
| idle RAM | 301.7MB + 357MB Docker | **30.9MB** | ~10x, ~21x incl. Docker |
| timeline (500 photos) | 111ms | **51ms** | 2.2x |
| one thumbnail | 11.2ms / 115KB | **1.4ms / 22KB** | 8x faster, 5x smaller |
| 60 thumbs, 12 parallel | 125ms / 6.2MB | **42ms / 1.1MB** | 3x faster, 5.6x less data |
| rename a 9-photo moment | 3min, then a 500 | **62-73ms** | ~2,500x |
| full client sync | - | 49s for 2,330 photos | |
| incremental index | - | 0.11s / 31.8MB for 2,321 unchanged | |

Derivative cache is 427MB for 8.5GB of originals (~5%), higher than PhotoView's 2.4% because
we store a 1400px preview as well as the thumbnail.

The rename figure is the one that matters most at scale: the old path cost a full-library
scanAll plus one whole-album-tree query per moved photo, so it got worse as the library grew,
regardless of how many photos actually moved. The new path is proportional to photos moved.

### What was actually built

- `server/indexer.py` - incremental, memory-bounded, resumable
- `server/appdb/media_schema.sql` - `media` table, indexed on `taken_at`
- `api.py`: `/timeline`, `/media/{id}/{thumb,preview,original,metadata}`, `/index`,
  `/login`, `/session`, `/logout`
- Client: `sync.ts` rewritten (no more `/photo-ids` resolve step), `db.ts` v6,
  `fullres.ts` + `MetadataPanel` repointed, video badges + `<video>` playback,
  `photoview.ts` deleted

### Deleted

`_scan_all_and_wait`, `_find_media_id_by_path`, `_reconcile_favorites`,
`import_favorites_from_photoview`, `set_favorite`'s PhotoView mirror, api.py's
`ReorganizeSlowScan` and both `scan_pending` branches, and the whole `admin_token`/`user_token`
split (which existed only because `scanAll` was admin-only while `favoriteMedia` was
user-scoped).

### Findings worth keeping

Four measured surprises shaped the indexer, none of which were visible from reading code:

1. `Image.draft()` only downscales in powers of two, and only when the target is at least half
   the source. `PREVIEW_MAX=1600` against a 3072px phone photo silently did **not** scale -
   full-size decode, 193MB. 1400 decodes at 1/2.
2. Operation order dominates peak memory: load/transpose/convert/thumbnail each allocate a
   buffer sized to what they operate on, so shrinking **last** makes every intermediate
   full-size. Shrinking first: 162MB -> 110MB.
3. Two separate drafted decodes beat one shared decode + `copy()`, because a shared decode has
   to be large enough for the bigger output. 193MB -> 37MB.
4. HEIC is the outlier: pillow-heif has no scaled-decode path, so one 24MP iPhone HEIC costs
   251MB. ffmpeg was tried and **rejected** - iPhone HEICs are grids of 512x512 tiles that
   ffmpeg 8.0 exposes as separate streams, so `-map 0:v:0` silently produced a single square
   tile. Caught only by checking output dimensions. Now decoded in a throwaway subprocess.

Also: this machine's ffmpeg has no webp encoder, so ffmpeg writes JPEG and Pillow re-encodes.
Assume nothing about which encoders a given ffmpeg build has.

### Final regression pass

Driven against the production build with PhotoView stopped and Docker down:

| | result |
|---|---|
| login -> first tiles | 361ms |
| full sync (cold IndexedDB) | 2,330 photos in 32s |
| viewer full-res swap | `fullres`, source badge "Server" |
| metadata panel | renders real EXIF |
| starred-only filter | works |
| grid density change | works |
| video badges | 6, with durations |
| video viewer | `readyState=4`, no error |
| server 5xx during run | **0** |
| console errors | **none** |

The one "failed request" Playwright reports is the browser aborting its own
range request on an HEVC stream it cannot decode - the server serves that same file
fine (200, 2.48MB; 206 + correct content-range on a ranged request).

### Known limitations

- **HEVC playback is Apple-only.** Confirmed in headless Chromium: `readyState=4`, no error,
  `videoWidth=0` - audio decoded, video track not. ~95% of the library is HEVC. The offline
  conversion route is written up above and unchanged.
- **Auth is one shared household password**, not per-user accounts. PhotoView's per-user model
  only ever scoped users to root paths, which this app does not use. Replaceable.
- **`sync.ts` still fetches every thumbnail.** Fine at 427MB; needs windowing before ~120GB.
