# Decision: visual design pass — light, Picasa-adjacent, customizable

## Request

"Once bare bones has been tested please make a modern ui that is light and customisable. I
want to keep roughly close to the user design of Picasa."

## What "Picasa-like" actually meant, concretely

Picasa's defining UI traits (from its actual historical design, not vague nostalgia): a light
neutral canvas, photos as the dominant visual element with minimal chrome around them, subtle
gray/white folder-style headers rather than heavy colored bars, star ratings as the primary
"reaction," and an uncluttered timeline that gets out of the way of the photos themselves.
Translating that into this project's existing architecture (React, CSS-in-JS inline styles
throughout the components built so far) rather than starting a redesign from zero.

## Design system approach

**CSS custom properties (variables) for theming**, defined once in `App.tsx`'s root/`index.css`
rather than continuing inline hex codes scattered across every component - this is what makes
it "customizable" per the request: a user (or a future settings panel) can override a small
set of named tokens (`--bg`, `--surface`, `--text`, `--accent`, `--radius`) to reskin the whole
app without touching component code. Kept deliberately small (a handful of tokens, not a full
design-token system) - matches "keep it light and simple."

**Light-first palette**: neutral off-white background (`#fafafa`, not stark white - easier on
the eyes for a photo-viewing app, still reads as "light"), photos framed with generous
whitespace/gap rather than tight grid lines, moment headers as understated gray-on-white text
rather than heavy bars, a single accent color (warm gold, echoing the existing ★ favorite
color already in use) reserved for interactive/selected states rather than decorating
everything.

**What did NOT change**: the actual layout structure (grid, virtualizer, viewer) - this pass is
a visual/styling pass on top of already-working, already-tested functionality, not a
structural rewrite. Restructuring to a left-side panel for login/sync (also requested) is a
separate, larger layout change - see docs/decisions/app-shell-layout.md.

## What got built

`client/web/src/theme.css` - the CSS custom properties + base resets (font, box-sizing,
color-scheme). Imported once in `main.tsx`. Existing components' inline styles updated to
reference `var(--token)` instead of hardcoded colors where it was a light-touch change;
left as-is where a deeper refactor would have risked destabilizing already-tested
interactions this late in the session - flagged inline with a comment for a future pass.
