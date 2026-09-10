# Auto-generate moments: gap-based regrouping, reusing dad's script's core idea

## Why this exists

The scale-test library (`server/fakelib/generate_scale_test_library.py`) copies real phone
photos preserving their original `YYYY/MM/` folder structure - flat calendar-month folders, not
this project's own `YYYY/YYYY-MM-DD-label` moment convention. That's fine for testing PhotoView
itself, but it means every real import needs a real regrouping step - exactly the problem
dad's `initial_script.py` was originally written to solve, just for this app's own reorganize
pipeline instead of an in-place DSM sort.

## What changed from `initial_script.py`

- **No forced year-boundary split.** Dad's `split_into_events` always starts a new batch when
  the calendar year changes, in addition to the gap check. This project made an explicit,
  earlier decision to reject that (see CLAUDE.md: "if a momemnt spills over then its fine to be
  in that same folder", and the archived plan.md's "Pure gap-based, no year split" decision) - a moment
  starting Dec 30 and running into January now stays one moment.
- **No geocoding at generation time.** Dad's script reverse-geocodes every event to build its
  folder name. This project's own decision (`docs/decisions/geo-reverse-geocoding.md`) keeps
  geocoding strictly opt-in, triggered by the user via the existing `/moments/suggest-name`
  endpoint - auto-generated moments get a plain `YYYY-MM-DD-moment` name, exactly like every
  other auto-created moment in this app (`create_moment` already does the same), and the user
  can rename/suggest-name afterward.
- **Goes through this app's own move pipeline** (`_move_photos_no_scan` + a single
  `_scan_all_and_wait`), not a raw `shutil.move` - so appdb/photo_id/favorites all stay
  consistent with every other reorganize operation, and the client's stable-id sync (see
  `docs/decisions/stable-photo-id.md`) picks up the result the same way it picks up any other
  move.
- **One scanAll for the whole run, not one per batch.** Confirmed directly (this session's own
  `/moments/create` testing at 2000-photo scale) that `scanAll` is a *global* rescan, not scoped
  to the changed paths - calling it once per gap-batch while regrouping a large flat folder
  into dozens of moments would mean minutes-per-batch, not minutes total. `move_photos` was
  split into `_move_photos_no_scan` (the filesystem/appdb half) + the existing scan-and-wait
  step, so `auto_generate_moments` can do all its moves first and pay the scan cost exactly
  once at the end.

## Robustness requirements (explicitly requested)

- **Idempotent / safe to re-run or point at a mixed tree**: any file already sitting under a
  path component that looks like an existing `YYYY-MM-DD-*` moment folder is skipped - so
  running this against a user's whole root (mixing already-organized moments and
  not-yet-organized flat folders) only touches the not-yet-organized photos.
- **A single unreadable/corrupt file never aborts the whole batch**:
  `read_capture_datetime` never raises - EXIF `DateTimeOriginal`/`DateTime`, falling back to
  file mtime (same fallback chain as dad's `get_photo_data`), always returns *something*.
- **Collision-safe destination naming**: two gap-batches landing on the same calendar date
  (two separate short trips both starting the 1st of a month, say) get `-2`, `-3`, ... suffixes
  rather than silently colliding or merging.
- **Dry-run mode**: `dry_run=true` computes and returns the full plan (destination folder names
  + photo counts) without touching the filesystem, so the client can show a preview before
  committing to what can be a large, slow (multi-minute at 2000+ photos) operation.
- **Slow-scan-after-real-success doesn't read as a failure**: reuses the `ReorganizeSlowScan`
  pattern added to `api.py` for `create_moment` - if the final `scanAll` confirmation times out
  (a real, observed failure mode at 2000+ photo scale, unrelated to whether the moves
  themselves succeeded), the endpoint still returns `ok: true, scan_pending: true` with the
  full plan, not a 500.

## What's exposed

`POST /moments/auto-generate` (`server/api.py`): `{source_dir, dry_run, gap_days}` ->
`{ok, scan_pending, moments: [{dest, count}]}`. `source_dir` is a path under `PHOTOS_ROOT`
(validated the same way every other endpoint validates client-supplied paths - see CLAUDE.md's
security invariant #6), typically a user's root or a subfolder of it (e.g. `"bob"` or
`"bob/2024"`).

## Not a client-triggered feature - runs periodically, server-side only

Explicit user correction during this session: this is **not** a button in the client UI. The
intended real-world usage is a periodic server-side job (e.g. monthly, after new photos have
been uploaded/synced onto the NAS) that runs `auto_generate_moments` automatically against each
user's root - the client never calls `/moments/auto-generate` directly, unlike every other
`/moments/*` endpoint.

**Scheduling mechanism deliberately deferred, not yet built**: two real options were
considered - an in-process scheduler inside `api.py` (self-contained, works identically in dev
and on the NAS) vs. DSM's own Task Scheduler invoking a standalone script/cron job (more
"native" to the eventual Synology deployment, but only genuinely testable on real DSM
hardware). The user's explicit call: implement this once actually on DSM hardware, not before -
correctly testing a scheduler's trigger mechanism needs the real environment this app targets,
not a guess made on a dev Mac. The endpoint itself is intentionally scheduler-agnostic (a plain
HTTP POST with no session/UI dependency) so either approach can call it unchanged once chosen.
