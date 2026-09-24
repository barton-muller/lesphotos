# Versioning

## The problem

There was no version at all: `client/web/package.json` sat at the create-vite default
`0.0.0`, `pyproject.toml` at the uv-init default `0.1.0`, no git tags, no changelog. That
made two things impossible: telling whether a NAS restart actually picked up the deploy
you think it did (see `deploy/README.md` and the restart steps in `CLAUDE.md` - previously
just `curl -I` for a 200), and telling, from a version number alone, whether upgrading
requires anything more than pulling and restarting.

## One version, not per-package

The client and server are **one deployed unit** - a single git checkout, built and served
by the same process (`api:root_app` serves both, see `CLAUDE.md`). They are never deployed
independently, so independent version numbers for them would track nothing real. There is
one `VERSION` file at the repo root; `pyproject.toml` and `client/web/package.json` mirror
it (kept in sync by hand at each bump - three short files, not worth a sync script for how
rarely this changes).

The **Dexie schema version** in `db.ts` (currently v9+) is a separate, unrelated number -
it versions the client's IndexedDB shape and bumps far more often, on its own schedule, via
its own `.upgrade()` migration path. Don't conflate the two.

## The scheme: `MAJOR.MINOR.PATCH`, defined for what this app actually is

Standard semver shape, but "breaking" is defined for a **self-hosted single-deploy app with
no external API consumers**, not for a published library:

- **MAJOR** - anything that needs a manual step beyond "pull and restart" to move to it
  cleanly. Concretely: an appdb schema change that needs `--prune` or a fresh index pass to
  read correctly; a cache format change that invalidates existing derivatives (forces a
  full re-derive); a Dexie schema bump whose `.upgrade()` can't run unattended and needs a
  documented manual step; a changed `manage.py`/`indexer.py`/`deploy/run.sh` invocation that
  breaks the exact commands in `CLAUDE.md`'s restart runbook or Task Scheduler entry.
- **MINOR** - a real feature or behavior change that a plain restart picks up cleanly. New
  endpoint, new UI, a Dexie migration that runs unattended, a changed default that doesn't
  strand old state.
- **PATCH** - a bug fix or UI tweak with no schema/contract change at all.

**Pre-1.0 (`0.x`)**: still true for the foreseeable future - this is a single-family app
with one deployment, not a library other people integrate against. Per standard semver
convention, a `0.x` MINOR bump can include what would be a MAJOR change above; there's no
compatibility promise to break yet. Move to `1.0.0` only if/when that promise starts to
matter - e.g. if the API is ever consumed by anything this codebase doesn't also control.

## When to bump

At a real milestone commit - the same bar `CLAUDE.md` already sets for committing at all
("commit at real milestones, not every save"), not every push. Concretely: bump `VERSION`
(+ mirror into `pyproject.toml`/`package.json`) and add a `CHANGELOG.md` entry in the same
commit that closes out the milestone, then `git tag vX.Y.Z` on it. Tagging is a manual,
deliberate action - never done automatically by an agent as part of an unrelated task.

## Why not date-based (CalVer) or a build counter

Both were considered. CalVer (`2026.09`) answers "when" but not "is this safe to skip to" -
the actual question a deploy needs answered, which is exactly what MAJOR/MINOR/PATCH
encodes. A bare build counter (incrementing integer) is even less informative and git's own
commit count/short SHA already gives that "which one exactly" answer for free (see
`tools/bench.py`, which tags every benchmark result with the git commit precisely because
"build 47" means nothing on its own). Semver's three-part shape is the one system that
still answers "what do I need to check before upgrading" from the number alone.
