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

## Remaining rollout: `lesphotos` service account (favoriting fix is done)

**Fixed and verified 2026-09-24**: starring a photo silently failed with a 500 whenever
the photo was one the server process couldn't read — see "Favorite/star toggling
500'd" below for the full root cause and fix. The `Bob/Photos` side is done and
confirmed working end-to-end (`POST /favorite` on a real, previously-broken photo now
returns `200`). This entry tracks what's left to finish the rollout:

- [ ] **`Alice/Photos` still needs the same treatment as `Bob/Photos`**: recursive
      `chgrp lesphotos-service`, then a *separate* recursive `chmod` pass (`g+rx` for
      directories, `g+r` for files — `chgrp` alone does not grant the group anything if
      the mode bits' group slot is `---`, which phone-uploaded files are by default; see
      the fix writeup below for why this is two passes, not one). `Alice/Photos` has no
      stray ACL (confirmed `synoacltool -get` says "It's Linux mode"), so it should be a
      clean run with no ACL cleanup step needed, unlike the false starts hit on `Bob`.
      Also still needed: `.lesphotos/appdb.sqlite3` (+ `-wal`/`-shm`) and `stars.json`
      need `chmod g+w`, not just `g+r` — the database needs to be written to, not just
      read.

      **Exact commands** (run over SSH as `alice@nas`, each `sudo` prompts for your own
      password; run the two `find` sweeps in the background and let each finish before
      starting the next — an interrupted sweep leaves the tree in a partially-migrated,
      inconsistent state, as happened once on the `Bob` sweep):

      ```sh
      sudo nohup find /volume1/homes/Alice/Photos -exec chgrp lesphotos-service {} \; \
        > /volume1/lesphotos/data/chgrp-alice.log 2>&1 &
      disown
      # wait for it: ps aux | grep "find /volume1/homes/Alice" | grep -v grep
      # (empty output = done)

      sudo nohup find /volume1/homes/Alice/Photos -type d -exec chmod g+rx {} \; \
        > /volume1/lesphotos/data/chmod-alice-dirs.log 2>&1 &
      disown
      # wait for it the same way, THEN:

      sudo nohup find /volume1/homes/Alice/Photos -type f -exec chmod g+r {} \; \
        > /volume1/lesphotos/data/chmod-alice-files.log 2>&1 &
      disown
      # wait for it, then finally:

      sudo chmod g+w /volume1/homes/Alice/Photos/.lesphotos/appdb.sqlite3 \
        /volume1/homes/Alice/Photos/.lesphotos/appdb.sqlite3-shm \
        /volume1/homes/Alice/Photos/.lesphotos/appdb.sqlite3-wal \
        /volume1/homes/Alice/Photos/.lesphotos/stars.json
      ```

      **Verify** before calling it done — log in as `alice` in the lesphotos client and
      favorite a recently-uploaded (not yet moment-organized) photo; a `200` back from
      `POST /favorite` is the actual proof, not just the `chmod` commands completing
      without error. Or check directly:
      `curl -X POST http://localhost:8001/lesphotos-api/favorite -b <alice's session
      cookie> -d '{"path": "...", "favorite": true}'`.
- [x] **The watcher deliberately stays `root` — not moving to `lesphotos`.** Decided
      2026-09-25: a plain `lesphotos` service account can `chgrp`/`chmod` files it
      owns, but a newly-arrived phone upload is owned by *the DSM user whose phone
      backed it up* (`Bob`, `Alice`, ...), not `lesphotos` — only `root` can fix
      permissions on a file it doesn't own. `handle_new_file` in `server/watcher.py`
      now calls `_grant_lesphotos_group_read()` right after every move: `chgrp
      lesphotos-service` + `chmod g+r` on the moved file, so new uploads become
      readable by the API server immediately, not just after a later manual sweep.
      Without this, every future phone upload would silently reproduce the exact
      favoriting-500 bug this investigation fixed, forever, for the newest photos.
      Resolves the gid once at import (`grp.getgrnam`, cached in `_LESPHOTOS_GID`),
      degrades to a no-op with one warning on a machine with no such group (e.g. a
      Mac dev checkout) rather than crashing file handling. Best-effort, not
      load-bearing: the move itself already succeeded even if this fails, and a
      later manual `chgrp`/`chmod` sweep (like the one used to fix the existing
      library) can always catch up.
- [x] **API server's Task Scheduler entry updated and verified, 2026-09-25.** "Run
      as" changed from `alice` to `lesphotos`; the "Run command" itself changed from
      `UV=/var/services/homes/Alice/.local/bin/uv /bin/bash
      /volume1/lesphotos/app/deploy/run.sh >> /volume1/lesphotos/data/server.log
      2>&1` to just swapping the `UV=` path — `UV=/usr/local/bin/uv /bin/bash
      /volume1/lesphotos/app/deploy/run.sh >> /volume1/lesphotos/data/server.log
      2>&1` — since Task Scheduler already runs the whole task as `lesphotos` now
      (no `sudo -u lesphotos` wrapper needed inside the command itself once the
      task's own "Run as" is set correctly), and `lesphotos` has no access to
      `Alice`'s personal `~/.local/bin/uv`. Verified live via a manual "Run" trigger
      from the Task Scheduler UI: new PIDs owned by `lesphot+`, clean
      startup/shutdown in `server.log`, `curl` against `:8001` returned `200`.
      **Still open**: the watcher's own Task Scheduler entry hasn't been checked/
      confirmed to already run with root privilege the same way (it was being
      started ad hoc via `sudo` over SSH, not necessarily via its own scheduled
      task) — verify that separately before assuming it survives a reboot too.
- [x] `deploy/README.md`'s restart section updated 2026-09-25 to match the verified
      Task Scheduler command above (see that file's "Making changes" section).

**Deliberately out of scope**: `Alice` (and any other DSM admin) retains implicit
access to every file on the NAS regardless of any of the above — inherent to the DSM
admin role, not fixable by chmod/group/ACL. Accepted: the goal was closing access
between ordinary (non-admin) family accounts, not stopping admins.

## Favorite/star toggling 500'd — root cause and fix (2026-09-24)

**Observed**: starring a photo on one device did not show up on another. The delta-sync
mechanism itself was confirmed working correctly first (tested directly against
`/favorite` + `/timeline?since=` on the NAS) — the actual bug was filesystem
permissions, not sync logic, so this took real digging to find.

**Root cause**: the server process (running as DSM user `Alice`) could not read files
owned by a *different* DSM user (e.g. `Bob`) with mode `700` — exactly what phone
photos get by default from the Mobile Backup app. `POST /favorite` calls
`db.content_hash_of(path)`, a plain `open(path, "rb")`, which raised `PermissionError`
→ an opaque FastAPI 500 → the star silently never saved anywhere. Confirmed live:
`manage.py list-users` showed "17 unreadable" for `bob`'s library; reproduced the
exact 500 on a real recently-uploaded photo before any fix.

Individual photo files being `700`/owner-only is *correct*, not the bug — the server
had too little access to files it legitimately needed to serve on behalf of their
owner, not the reverse. Separately, this investigation also found `Bob/Photos` and
`Alice/Photos` were group-owned by DSM's own `SynologyPhotos` group (auto-created by
the Synology Photos app for its own "Shared Space" feature, not something either of us
configured deliberately) — meaning any DSM user in that group (every family member)
could browse another's `Photos/` folder directly over SMB/File Station/SSH, bypassing
the app entirely. Confirmed nobody uses that DSM feature; closed as part of this fix.

### Approach: a dedicated `lesphotos` service account, plain Unix group, own venv

A new DSM account, `lesphotos` — **non-admin** (DSM admins bypass all file permissions
outright, so making it admin was tried once, found to defeat the entire point, and
reverted before it was used for anything) — now runs the API server. **The watcher
deliberately stays `root`**, not `lesphotos` — see "Remaining rollout" above for why:
it needs to fix permissions on files it doesn't own (a new phone upload arrives owned
by whichever DSM user's phone backed it up), which only `root` can do. Filesystem
access for the API server is granted via a **plain Unix group**, `lesphotos-service`,
containing only `lesphotos`, set as each `Photos/` folder's group.

**Two chmod passes were needed per library, not one** — this was the single biggest
time-sink of the whole fix, worth remembering clearly:
1. `chgrp lesphotos-service` on every file/folder, recursively.
2. A *separate* `chmod g+rx` (directories) / `g+r` (files) pass — because the existing
   mode bits on phone-uploaded files were `700` (group slot `---`), `chgrp` alone
   changes *which* group owns the file, not *what that group can do*. Skipping step 2
   is why the very first end-to-end retest after step 1 still 500'd.
3. On top of that, `.lesphotos/appdb.sqlite3` (+ its `-wal`/`-shm` siblings) and
   `stars.json` specifically needed `g+w` (not just `g+r`) — the app writes to these,
   not just reads them, which the earlier passes (aimed at photo files) didn't cover.

**`.venv`, `uv`, and `node_modules` all had the same class of bug**: each was
originally created by `Alice`, and critically, `uv`'s own downloaded Python interpreter
lives by default under the *invoking user's home directory*
(`~/.local/share/uv/python/...`) — so even once `lesphotos` had access to the project
checkout itself, `.venv/bin/python` was a symlink into `Alice`'s personal home, which
`lesphotos` had no reason to ever access. Fix: `lesphotos` built its own, fully
independent `.venv` (`sudo -u lesphotos uv sync` after `rm -rf .venv`) and its own
`node_modules` (same `rm -rf` + reinstall pattern) — no shared dependency on any one
person's home directory. `uv` itself (the launcher binary) also needed to stop being a
personal-home-directory binary — copied to `/usr/local/bin/uv` (`755`, world-executable,
matching how `node`/`npm` already lived there) rather than granting `lesphotos` access
into `Alice`'s `~/.local/bin/`.

**`deploy/lesphotos.env` (the file holding `LESPHOTOS_SECRET`)** was `600 Alice` —
`lesphotos` couldn't read it to start the server at all. Fixed by transferring
ownership to `lesphotos` (`chown lesphotos`, kept at `600`) rather than widening its
group — this is a session-signing secret, so narrowing access to exactly the account
that needs it (and removing `Alice`'s own access, which he no longer needs once he's
not the one running the server) was the right call, not a compromise.

**DSM's ACL system (`synoacltool`) was tried first and abandoned** in favor of the
above plain-Unix-group approach — worth documenting clearly since it cost real time and
might tempt a future fix attempt to reach for it again:
- An `owner:*:allow` ACL entry did **not** grant the file's actual owner read access,
  even after also trying an explicit `user:<owner-name>:allow` entry naming the correct
  uid directly. Root cause was never found; `synoacltool -check`/`-get-perm` confirmed
  the owner had zero effective permission despite the ACL clearly listing an allow
  entry for them.
- `-enforce-inherit` did **not** propagate a folder's ACL down to files that already
  existed inside it, despite that being the documented purpose of the flag.
- `synoacltool -add` on an individual **file** *replaces* its classic Unix mode bits
  rather than adding to them — observed dropping a file straight to mode `0000`
  (unreadable by literally everyone, including root via plain `cat`, though `sudo`
  still worked) the moment the first ACL entry was added.
- The same replacement behavior hit a **directory** too: `/volume1/homes/Bob/Photos`
  itself was left in a broken `d-----S---+` state (no usable access for anyone) after
  an early `synoacltool -add` experiment on it — had to be recovered with
  `synoacltool -del <index>` (once per added entry) followed by a plain `chmod 2775`
  to restore normal owner/group/other bits.
- DSM's GUI "Convert to Windows ACL" wizard (Control Panel → Shared Folder → Edit →
  Permissions) was found and *not* used, deliberately — it's folder-wide and marked
  "irreversible" by DSM's own dialog; the ACL problems above were reason enough not to
  risk it on real data mid-investigation.
- One thing the GUI *did* get right, on the `lesphotos` shared folder specifically
  (the app's own code checkout, not a photo library): granting `lesphotos` Read/Write
  via Control Panel → Shared Folder → `lesphotos` → Edit → Permissions correctly
  propagated down to files that already existed, unlike every manual `synoacltool -add`
  attempt on the photo libraries. Why the GUI succeeded where the CLI kept failing was
  never fully explained — plausibly a different, more thorough code path DSM's own
  wizard takes that `-add`/`-enforce-inherit` don't.

**SSH access is DSM-restricted to the `administrators` group, with no per-user
override** (confirmed directly via DSM's own SSH service dialog text) — so `lesphotos`
cannot log in over SSH itself, and making it an admin (which *would* grant SSH) was
exactly the wrong fix, per above. This is fine: the account only ever needs to *run
processes*, not have an interactive login. Two ways to do that without SSH access of
its own: DSM Task Scheduler's "Run as" user (confirmed `lesphotos` appears in that
dropdown), or `sudo -u lesphotos <command>` from an admin's own already-authenticated
SSH session — both were used interchangeably during setup and testing.

See "Remaining rollout" above for what's left (`Alice/Photos`, the watcher, and Task
Scheduler still need the same treatment).
