# Decision: metadata panel — what PhotoView gives us for free vs. what's missing

> **Partly superseded.** Still lazy-fetched per photo, but EXIF now comes from our own
> `/media/{id}/metadata` (read on demand with Pillow) rather than PhotoView's `media(id:)`
> GraphQL query.

## Research summary (full detail in the archived plan.md)

PhotoView's `MediaEXIF` type exposes: `description`, `camera`, `maker`, `lens`, `dateShot`,
`exposure`, `aperture`, `iso`, `focalLength`, `flash` (raw code), `exposureProgram` (raw code),
`coordinates`. File size and dimensions live on `Media.highRes`/`thumbnail` (`MediaURL.fileSize`),
not on `MediaEXIF`. Videos get a separate `VideoMetadata` type (width, height, duration, codec,
framerate, bitrate, colorProfile, audio).

**Missing, confirmed not present anywhere in the schema**: original filename (only `title`,
which is filename-derived but not guaranteed identical), file format/mime type, orientation,
white balance, metering mode, GPS altitude, lens maker/serial (only a lens model string),
human-readable flash/exposure-program (only raw EXIF integer codes needing a decode table).

## Decision: build the panel with what's available now, decode the raw codes client-side,
## skip re-implementing full EXIF extraction for the missing fields

Reasoning: ~80% of what a typical photo-info panel wants (camera, lens, exposure triangle,
ISO, focal length, GPS, date, file size, video duration/codec) is already exposed with zero
extra work. The missing pieces (filename, orientation, altitude, etc.) are minor/cosmetic for
this use case — not worth adding a second EXIF-reading pipeline (client-side `exifr` on the
downloaded full-res bytes, or extending PhotoView's own schema) purely to fill them in, given
the project's "keep it light and simple" direction. Revisit only if the missing fields turn
out to actually matter in practice (e.g. GPS altitude for hiking trip photos), not
speculatively now.

`flash`/`exposureProgram` are raw EXIF integer codes (not decoded by PhotoView) - decoded
client-side against the standard EXIF spec lookup tables in the panel component, so the UI
still shows "Flash: Off, did not fire" rather than "Flash: 16".

## What got built

`client/web/src/photoview.ts`: `fetchMediaDetail(id)` - a lazy, per-photo GraphQL query
(`media(id:)`), fetched only when the metadata panel is actually opened, not bundled into the
bulk timeline sync (would be wasted work for the vast majority of photos never inspected this
closely). `client/web/src/MetadataPanel.tsx`: renders the fetched fields, with graceful
"—" placeholders for anything null (very common for phone photos lacking full EXIF, or PNGs
with no camera data at all - confirmed directly against real test photos during this build).
