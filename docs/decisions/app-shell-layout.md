# Decision: left-panel app shell layout

> **Superseded.** The left sidebar described here was dropped — the app is now a full-bleed
> grid with a bottom status bar, and the rail is a pull-out handle on narrow screens. Kept
> for the reasoning about ambient sync (still true: sync is automatic, never a user step).

## Request

"It should look nice in future with left panel for login, sync (which should be automatic on
open+rename...really we shouldn't see it in final version maybe)."

## Design

Restructure from the current top-header-bar layout to a persistent left sidebar (desktop) /
bottom-adjacent drawer (narrow viewports - a left sidebar eating horizontal space is a worse
tradeoff on a phone screen than on desktop) holding: account/login state, sync status
(collapsed to an icon + subtle indicator most of the time, per "shouldn't see it in final
version" - sync becoming ambient/automatic rather than a manual action point), and the
starred-only toggle. The timeline/grid becomes the dominant central content area, matching the
"photos as the dominant visual element" principle from the visual design pass.

**Sync-on-open**: `App.tsx` triggers `syncTimeline()` automatically once logged in (on mount /
after login succeeds), rather than requiring the manual "Sync" button click that's been the
whole testing session's convenience affordance. The button itself is kept, but demoted to a
small manual-refresh icon in the sidebar rather than a primary action - still available for
forcing a re-sync (useful for testing, and for a user who wants to confirm they're fully
up to date), but no longer something the user needs to think about routinely.

**Why the button isn't removed entirely**: the app has no push/websocket mechanism to know
when new photos exist server-side - periodic auto-sync (e.g. every N minutes, or on window
focus) is a reasonable next addition but wasn't explicitly requested and adds its own
complexity (background timers, avoiding redundant concurrent syncs); a manual refresh affordance
covers the gap safely in the meantime.

## What got built

`client/web/src/App.tsx` restructured: a `<Sidebar>` component (login form collapses to a
small account chip once logged in; sync status becomes a small spinner/checkmark rather than
inline text; starred-only toggle moves here) alongside the main `<Timeline>` content area.
Auto-sync triggered via a `useEffect` on login success. This is a layout/composition change,
not a change to any of `Timeline.tsx`'s internal logic (grid, selection, viewer) - those
continue working exactly as already tested.
