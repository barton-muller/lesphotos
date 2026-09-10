# Live Photos and Motion Photos

**Status: designed, not built.** The findings here are measured against the real
library; the implementation is not started.

## The problem

Half the family shoots iPhone, half Pixel. Both produce "a still with a short
clip attached", and the app currently handles neither — with opposite symptoms.

On the real library (`/volume1/homes/Alice/Photos`, 8,637 files):

- **1,604 of 1,650 `.MOV` files are Live Photo pairs** — 97%. Each appears as a
  *second tile* beside its still, with a near-identical thumbnail and the same
  capture time. Roughly 19% of the library is duplicated this way.
- Google files are `PXL_*.MP.jpg` and show correctly as one tile already.

## The two formats are not the same shape

|  | Apple Live Photo | Google Motion Photo |
|---|---|---|
| Files on disk | **two** — `IMG_1234.HEIC` + `IMG_1234.MOV` | **one** — `PXL_….MP.jpg` |
| How they pair | `ContentIdentifier` (HEIC MakerNote) matches `com.apple.quicktime.content.identifier` (MOV) | video embedded inside the JPEG |
| Locating the video | it is its own file | `GCamera:MotionPhoto` XMP, with a `Container:Directory` giving the byte offset |
| Symptom today | duplicate tiles | none — already one tile |

Measured on a sample `PXL_*.MP.jpg`: 3,409,909 bytes total, `ftyp` box at
1,870,574, so **45% of the file is an MP4** that the app currently ignores.

## What was decided

**Keep the pair, play the motion on long-press, move them together.**

Not "hide the clip and forget it": the motion is the point of the format, and
discarding it would quietly lose data the camera captured.

Three pieces:

1. **A `motion` relationship in appdb**, keyed by `photo_id` — the still is
   canonical and carries an optional pointer to its motion source. Not a
   filename convention: names change (invariant #3 moves files through the app),
   and Google has no second file to name.

2. **Two detectors in the indexer**, one per format. Apple's pairs on the
   content identifier, *not* on matching basenames — a renamed library would
   silently stop pairing, and basename matching is a guess where the identifier
   is what the camera actually wrote. Google's reads the XMP offset.

3. **One client concept**: a still that knows it has motion. Long-press plays it.
   Both formats collapse to this, which is what makes it one feature rather than
   two.

The Apple `.MOV` stops being its own timeline entry; the Google file needs no
timeline change at all.

## Consequences to handle

- **Moves must keep pairs together.** `reorganize.py` moves files; an Apple pair
  is two files that must not be separated. This is the main reason the
  relationship lives in appdb rather than being recomputed from the filesystem.
- **Serving Google motion is a byte-range read**, not a decode — the offset is in
  the XMP. Cheap, and it works on the NAS, which has no video decoder at all
  (see `platform_hosts.py`).
- **The 46 non-pair `.MOV`s are real videos** and must keep behaving as they do
  now.
- **Existing libraries need a re-index** to populate the relationship. The
  derivatives are unaffected, so it is a metadata pass, not a re-derive.

## Playback

Two distinct gestures, deliberately:

- **Play button** (beside the info button) — toggles: press to start looping,
  press again to stop.
- **Long-press on the image** — plays only while held, stopping on release.

## Download: no cross-platform container exists

Confirmed, not assumed: **iOS Photos does not animate Google Motion Photos.** It
renders the still and ignores the appended MP4 entirely, because it does not
parse Google's `GCamera:MotionPhoto` XMP. There is therefore no single file that
animates on both platforms.

So download preserves what the photo already is:

- an Apple pair downloads as its `.HEIC` + `.MOV`, which iOS re-pairs on import
- a Google file downloads as the single `.MP.jpg` it already is

Each animates on its native platform, nothing is converted, nothing is lost. The
alternative — picking one format — would break animation for half the family.

**Future work:** converting between them is possible (see MotionPhoto2, which
muxes Apple pairs into Google/Samsung motion photos), so a download could match
the *requesting* platform rather than the source. Worth doing only if sharing
across platforms turns out to matter in practice.

## Not decided yet

Whether a Live Photo's `.MOV` should be separately downloadable — not for now —
and what long-press means on desktop, where there is no touch-and-hold.
