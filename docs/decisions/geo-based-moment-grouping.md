# Testing note: geo-based moment grouping

## What was asked

"We also need to test geo based moments" - checking whether location data should factor into
how photos get auto-grouped into moments, not just the existing time-gap heuristic
(`server/fakelib/grouping.py`, adapted from dad's script).

## Finding: this is already effectively handled, by design

The existing gap-based grouping (`group_into_moments` in `grouping.py`) deliberately does
**not** use location to decide moment boundaries - this was an explicit decision inherited
from dad's script and confirmed early in this project: "Location plays no part in grouping:
an event crossing a town boundary stays one event, and the place name only reaches the name."

This is the right call, not an oversight: a single day trip through several towns (or a long
train journey) is still one moment by the user's own mental model, even though GPS coordinates
change continuously throughout it. Using location to *split* moments would fragment things
the time-gap heuristic correctly keeps together. Location's actual role is **naming** a
moment after it's already been formed by time-gap clustering (`server/geocoding.py`,
`suggest_moment_label` - picks the location of the *first* GPS-tagged photo in the moment as
representative), not deciding the grouping itself.

**What was tested tonight**: confirmed real GPS-tagged photos (from the scale-test library,
real coordinates around Delft/Rotterdam, Netherlands) correctly cluster into moments via the
existing time-gap logic and correctly produce a sensible geo-suggested name for the resulting
moment (`03-strausspad.delft.nl`) - see the geo-reverse-geocoding section of the archived plan.md for the
full test. This confirms the pipeline works end-to-end on real location data, not just that
the two pieces (grouping, naming) exist independently.

## Not built: location-based moment *splitting*

If a future need arises to split a moment specifically because its photos span a large
geographic distance in a short time (e.g. photos from two different cities on the same day,
currently kept as one moment by the time-gap rule) - that's a distinct feature from what was
asked for tonight, and the existing manual tools already cover it: the range-select "create
moment from selected photos" feature (see docs/decisions/range-select-new-moment.md) lets a
user manually split any moment by location or any other criteria they notice, without needing
an automatic geo-distance-based splitting rule. Not building an automatic version of this
unless it turns out to be a recurring annoyance in practice - matches the "keep it light and
simple" direction.
