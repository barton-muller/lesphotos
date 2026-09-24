# Known issues

Bugs observed in real use, not yet root-caused or fixed. One entry per issue: what was
observed, what was expected, and any leads. Move an entry to a commit message (and delete
it here) once it's actually fixed — this file is a queue, not a changelog.

## TODO — UI papercuts (reported 2026-09-24)

- [ ] **Share button shows a status message after sharing.** Not needed — the OS share
      sheet is its own confirmation. Lead: `PhotoViewer.tsx` sets `shareStatus` from
      `shareOrDownloadPhoto`'s result (`'Shared'` / `'Saved'` / `'Downloaded'`) and renders
      it as `share-status`. Drop the message for `'shared'`; keep it for the download
      fallbacks, where there is no other feedback that anything happened.
- [ ] **Android: pinch on the timeline zooms the page instead of changing tiles per row.**
      Lead: `usePinchZoom.ts` listens to pointer events but the scroll element sets no
      `touch-action`, so Chrome claims the two-finger gesture as native pinch-zoom and
      fires `pointercancel` before our handler sees any movement. Likely fix:
      `touch-action: pan-y` on the timeline scroll element (keeps vertical scroll, hands
      pinch to JS). Verify on a real Android device — desktop emulation doesn't reproduce
      native pinch arbitration.
- [ ] **Android: +/− density buttons push Select / Merge moments into a horizontal
      scroller.** `#lesphotos-toolbar-slot` in `App.tsx` is `flex: 1; overflowX: auto`, so
      on a narrow header those controls end up behind a scrollbar. Once pinch works (above),
      hide the +/− buttons on touch devices (`@media (pointer: coarse)`) to free the room;
      otherwise collapse Select/Merge to icons.
- [ ] **"Download Thumbnails" is too long for the account menu.** Shorten to icon +
      "Thumbs" (download icon); the existing `title` tooltip already carries the full
      explanation. `App.tsx`, `data-testid="sync-all"`.
- [ ] **Android: grid appears to refresh when favouriting.** Lead: `Timeline.tsx`'s
      load effect depends on `tableVersion` (bumped by any `db.photos` write, including
      `toggleFavorite`) and starts with `setPhotos(undefined)`, then re-reads the whole
      table in two pages. So every star blanks and rebuilds the grid; a fast desktop hides
      it, a phone doesn't. Fix: don't clear `photos` on a version bump (only on
      `starredOnly` change), or patch the single row in place for a favourite toggle.
      Check it also preserves scroll position.

## Favorite/star toggling does not sync across devices — permissions fix in progress

**Observed 2026-09-24, root-caused same day**: starring a photo on one device did not
reliably show up as starred on another. The delta-sync mechanism itself is confirmed
working correctly (tested directly against `/favorite` + `/timeline?since=` on the NAS)
— the actual bug is filesystem permissions, not sync logic.

**Root cause**: the server process (running as DSM user `Alice`) cannot read files
owned by a different DSM user (e.g. `Bob`) with mode `700` — exactly what phone
photos get by default from the backup app. `POST /favorite` calls
`db.content_hash_of(path)`, which does a plain `open(path, "rb")` — this raises
`PermissionError`, which FastAPI turns into an opaque 500, so the star silently never
saves anywhere. Confirmed live: `manage.py list-users` showed "17 unreadable" for
`bob`'s library; reproduced the exact 500 on a real recently-uploaded photo.

Individual photo files are correctly private (`700`, owner-only) — this is NOT a
"photos are too open" bug, the opposite: the server has too little access to files it
legitimately needs to serve on behalf of their owner. Separately, this investigation
also found `Bob/Photos` and `Alice/Photos` were group-owned by DSM's own
`SynologyPhotos` group (auto-created by the Synology Photos app for its own
cross-account "Shared Space" feature, not something configured deliberately) — meaning
any DSM user in that group (every family member) could browse another's `Photos/`
folder directly, bypassing the app entirely. Not used deliberately, closed as part of
this fix.

**Approach chosen**: a dedicated `lesphotos` DSM service account (non-admin — DSM
admins bypass all file permissions, so making it admin would defeat the point, and was
tried and reverted), granted access via a **plain Unix group**
(`lesphotos-service`, containing only `lesphotos`) set as each `Photos/` folder's
group, `chmod 2775` (setgid, so new files inherit the group automatically). **DSM's
ACL system (`synoacltool`) was tried first and abandoned** — it produced confusing,
undocumented behavior (an `owner:*:allow` entry did not grant the file's actual owner
read access even when explicitly named by uid; `-enforce-inherit` did not propagate a
folder's ACL to existing files as its name implies) that consumed significant
debugging time without a working result. `synoacltool -add` was also found to
*replace* a file's classic Unix mode bits (e.g. dropping a file to `0000`) rather than
add to them, which is a sharp edge worth remembering if ACL is ever revisited — restore
with `chmod` + `synoacltool -del <index>` for every added entry, not just one.

SSH access is DSM-restricted to the `administrators` group with no per-user override
(confirmed via DSM's own service dialog), so `lesphotos` cannot log in via SSH itself —
this is fine and expected, since it never needs to: the account only needs to *run
processes*, launched either via DSM Task Scheduler's own "Run as" user (confirmed
`lesphotos` is selectable there) or via `sudo -u lesphotos <command>` from an admin's
own SSH session.

**Status as of 2026-09-24**:
- [x] `lesphotos` DSM account created (non-admin, `users` group only)
- [x] `lesphotos-service` Unix group created, `lesphotos` added as its only member
- [x] `Bob/Photos` fully migrated: recursive `chgrp lesphotos-service`, top folder
      restored to clean `drwxrwsr-x` (`2775`) after an earlier ACL experiment had left
      it in a broken ACL-only state (`d-----S---+`, unreadable by everyone including
      the owner) — restored via `synoacltool -del <index>` + `chmod 2775`. Verified:
      `lesphotos` can read a previously-`700` file via group permission alone.
- [ ] **`Alice/Photos` still needs the same recursive `chgrp lesphotos-service` sweep**
      (command ready, not yet run — user wants to verify the `Bob` side works
      end-to-end first). Run: `sudo nohup find /volume1/homes/Alice/Photos -exec chgrp
      lesphotos-service {} \; > /volume1/lesphotos/data/chgrp-alice.log 2>&1 &
      disown` (background — do not run in foreground / do not Ctrl-C, an interrupted
      sweep leaves the tree in a partially-migrated, inconsistent state, which happened
      once already on the `Bob` sweep and had to be finished via a second pass).
      `Alice/Photos` currently has no stray ACL (confirmed "It's Linux mode"), so no
      cleanup step should be needed after the sweep, unlike `Bob/Photos`.
- [ ] Point the running API server and watcher processes at `lesphotos` instead of
      `Alice`/`root` (Task Scheduler "Run as", or `sudo -u lesphotos` for manual runs).
      Not yet done — current production processes still run as `Alice` (server) and
      `root` (watcher).
- [ ] Re-verify the original bug (favorite a recent phone photo) is actually fixed
      end-to-end once the server process itself runs as `lesphotos`.
- [ ] Remove this entry once verified fixed and the above are all checked off.

**Deliberately out of scope for this fix**: `Alice` and other DSM admins retain
implicit access to every file on the NAS regardless of any of the above — that's
inherent to the DSM admin role, not fixable by chmod/group/ACL. Accepted: the goal was
closing access between ordinary (non-admin) family accounts, not stopping admins.
