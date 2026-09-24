# Known issues

Bugs observed in real use, not yet root-caused or fixed. One entry per issue: what was
observed, what was expected, and any leads. Move an entry to a commit message (and delete
it here) once it's actually fixed — this file is a queue, not a changelog.

## Android pinch on the timeline — fixed, needs a real-device check

**Reported 2026-09-24**: pinching the grid on Android zoomed the page instead of changing
tiles per row. Fixed in `usePinchZoom.ts` (touch events with a non-passive `touchmove`, plus
`touch-action: pan-x pan-y` on the scroll element). Verified under Chromium's Pixel 7
emulation with a CDP-synthesized pinch, but that emulation did **not** reproduce native page
zoom even before the fix, so the original symptom is only proven gone on a real phone.
Delete this entry once confirmed there.

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
