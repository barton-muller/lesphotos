# Decision: the timeline rail (jump-to-date scrubber)

Supersedes four earlier iterations (`timeline-year-scrubber`, `-v2`, `-v3`, `-v4`), which were
consolidated into this file. The problem statement never changed; the design did, four times,
each because a screenshot at real data scale showed the previous one didn't work.

## Request

"Is there a nice way to scroll the timeline, e.g. I want to go back a couple years — maybe
markers of years and big moments/locations, so I could reference 'oh, the photo was in 2024'."

## What it does

A fixed rail at the right edge showing where you are in time, and letting you jump. Years are
spread down the rail; the months of the year you're in expand into a band around the current
position, so nearby time is legible while distant time stays compact.

## Design

`client/web/src/rail/layout.ts` is **pure** — no React, no DOM. Given a continuous position in
"absolute months" plus a rail height, it returns the ticks to draw. That separation is what
makes the geometry testable and keeps per-frame work trivial.

The whole thing rests on one idea:

> Months live in a fixed-size band whose **slots never move** — labels scroll *through* them.
> Years are placed by a linear year→pixel map with a **hole cut out of it** exactly the height
> of that band. The hole slides down the rail as you scroll back in time, which keeps years
> evenly spaced on each side while the band pushes them apart.

The indicator stays roughly still and the content moves past it, rather than a marker sliding
down a static list. That's closer to how iOS Photos behaves and it avoids the indicator
spending most of its life at one end of the rail.

`monthBuckets` (one entry per calendar month that has photos, with its row index and busiest
moment) is computed **once** from the timeline, not per scroll frame.

## Why it took four passes

Each earlier version was replaced for a reason worth remembering:

- **v1** — static year labels beside the scrollbar. Read as decoration; no sense of position.
- **v2** — continuous track with a moving indicator. Better, but at ~2,000 photos the labels
  collided into unreadable overlap.
- **v3** — distance-based label density and a solid track. The collision maths and the track
  colour both looked correct in code and were **plainly broken in a screenshot**: labels still
  overlapped, and the track was too close to the page background to read as solid.
- **v4** — fisheye with 20 fixed slots. Worked, but the year and month layers fought each
  other; spacing was derived from the same scroll position twice, so they drifted apart.
- **v5** (current) — the year map with a hole in it, which makes the two layers one
  calculation instead of two that must agree.

**The recurring lesson:** every one of these failures was invisible from reading the code and
obvious in a screenshot at real library size. A 30-photo test library exercises none of the
label-collision behaviour that breaks at 2,000.

## Current state

`rail/RailScrubber.tsx` + `rail/layout.ts`, selected by `const SCRUBBER: 'v4' | 'v5' = 'v5'`
in `Timeline.tsx`. `YearScrubber.tsx` (v4) is still present behind that flag as a fallback and
because it exports `computeMonthBuckets`, which both versions use.

On a phone the rail collapses to a 28px pull-out handle at the screen edge, so it stays a
usable thumb target without eating grid width.
