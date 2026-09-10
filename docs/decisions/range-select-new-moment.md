# Decision: selecting photos to form a new moment

## Context

User wants to select a range/set of photos (likely spanning what the auto-grouping put into
separate moments, or a subset of one moment) and create a new moment from them - the manual
override for whatever the geo/gap-based auto-sorting produced. The server side
(`reorganize.move_photos`) already supports moving arbitrary photos into a (possibly new)
destination folder; this is purely a client-side selection UI + one more API endpoint.

## Design

**Selection model**: a lightweight "selection mode" toggled from the header (not always-on
checkboxes cluttering every thumbnail) - tap a "Select" button, then tap photos to
add/remove them from the selection (checkmark overlay appears), tap "Create moment" to act on
the selection. Chosen over a drag-select rectangle (harder to get right on touch, and the
photos are virtualized/not all mounted at once, which makes a spatial drag-select
meaningfully harder to implement correctly against a windowed grid) or a "shift-click range"
model (ambiguous once the grid re-flows post-filter/resize, and less touch-friendly than tap
selection, which matters given phones are the primary target device).

**New API endpoint**: `POST /moments/create` - takes a list of photo paths and a new moment
label, creates the destination folder, moves the photos in (reusing `reorganize.move_photos`,
which already does exactly this), scans, reconciles favorites. This is really just
`move_photos` exposed with a "make me a new folder for these" framing rather than "move into
this existing folder" - the existing `move_photos` already creates the destination if it
doesn't exist (`dest_moment_path.mkdir(exist_ok=True)`), so no server-side logic changes were
needed, only a thin endpoint wrapping the existing function with a naming convention for the
new folder (date-prefixed from the earliest selected photo, matching `merge_moments`'s
existing convention).

**What happens to photos removed from their original moment**: if a selection empties a moment
entirely, that moment's folder is left empty on disk (not auto-deleted) - matches
`merge_moments`'s existing behavior of only removing folders it explicitly emptied itself via
its own move. An empty original folder disappearing from the timeline is just PhotoView not
showing empty albums by default; no cleanup step was judged necessary for this pass. Revisit
if empty leftover folders turn out to be a real annoyance during actual use.

## What got built

- `server/reorganize.py`: no change needed - `move_photos` already does exactly this.
- `server/api.py`: `POST /moments/create` - thin wrapper, takes `photo_paths: list[str]` and
  `new_label: str`, derives the destination folder name from the earliest photo's parent
  moment (or today's date if selecting across moments with different dates - matches
  `merge_moments`'s "earliest date wins" convention).
- `client/web/src/api.ts`: `createMomentFromPhotos(photoPaths, newLabel)`.
- `client/web/src/Timeline.tsx`: selection-mode toggle, per-thumbnail selection overlay, a
  "Create moment" action bar that appears once ≥1 photo is selected.
