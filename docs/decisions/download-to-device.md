# Decision: "download to device" for sharing (e.g. to WhatsApp)

## Request

A way to save a photo's original onto the device itself (not just view it), for sharing
through other apps (WhatsApp etc.), landing in a folder structure that mirrors the server's
own layout.

## Design constraints (browser PWA, not a native app)

A web app cannot write directly into the OS's arbitrary filesystem/photo library the way a
native app can. The realistic options in a browser:

1. **`<a download>` / Blob URL trigger** - browser's own "Save As"/downloads folder. Universal,
   works everywhere, but lands in the browser's flat Downloads folder, not a mirrored
   directory structure - browsers do not let a web page choose an arbitrary destination path
   for security reasons.
2. **File System Access API** (`showSaveFilePicker`/`showDirectoryPicker`) - lets the user
   grant the page a real directory handle it can write nested files into, actually enabling a
   mirrored folder structure on-device. **Chrome/Edge only** (desktop; limited/no support on
   iOS Safari, which matters since iPhone is confirmed as one of the target devices) - a real
   gap, not a bug in the design.
3. Native share sheet (`navigator.share` with files) - hands the photo directly to WhatsApp
   etc. without a "save then attach" step at all, which arguably serves the *actual* underlying
   need (share to WhatsApp) better than a literal folder-mirroring download. Well-supported on
   iOS Safari and Android Chrome, less relevant on desktop.

## Decision: layer both — share sheet first, folder-mirrored download as a fallback/explicit option

Reasoning: the user's stated *goal* was sharing to WhatsApp, with "mirror the server's folder
structure" as the mechanism they described for a literal file-system download - but on the
primary target devices (phones, per "we should have option to download onto device e.g. if
user wants to share to whatsapp"), `navigator.share` directly hands the image to WhatsApp's
own share target in one tap, which is strictly better UX than save-to-folder-then-attach.
Given iOS Safari's limited File System Access API support, a folder-mirrored save can't be the
*only* path without failing outright on iPhone - one of the two explicitly named target
devices. So: **primary action is a Share button** (native share sheet, works everywhere that
matters), **secondary action is Download** using the File System Access API where available
(desktop Chrome/Edge - genuinely gets a mirrored folder tree) with a plain `<a download>`
fallback elsewhere (flat Downloads folder, browser's own location - the best that's achievable
on unsupported browsers, not a silent failure).

## What got built

- `client/web/src/download.ts`: `shareOrDownloadPhoto(photo, blob)` - tries
  `navigator.share({files: [...]})` first if the browser supports sharing files; falls back to
  `showDirectoryPicker`-based mirrored save (constructing `<moment>/<filename>` under a
  user-granted root directory, remembered across the session via a held directory handle so
  repeated downloads don't re-prompt for a folder every time) if available; falls back further
  to a plain `<a download>` Blob URL trigger as the universal last resort.
- `PhotoViewer.tsx`: a share/download button using the already-fetched full-res blob (no extra
  network round-trip - reuses whatever `getFullRes` already loaded for viewing).
