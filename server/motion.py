#!/usr/bin/env python3
"""
Live Photos (Apple) and Motion Photos (Google): detection and extraction.

Both are "a still with a short clip attached", but they are stored in
completely different shapes, and the app has to reconcile them into one
idea - a still that knows it has motion.

  Apple:  two files. IMG_1234.HEIC + IMG_1234.MOV, paired by a
          ContentIdentifier UUID that appears in both. On the real
          library, 1,604 of 1,650 .MOV files are halves of such a pair.

  Google: one file. PXL_….MP.jpg is a complete JPEG with a complete MP4
          appended, located by GCamera XMP. Measured on a sample: 3.4MB
          total, video starting at byte 1,870,574 - 45% of the file.

Pairing is done on the identifier the camera wrote, never on matching
basenames: names change (invariant #3 moves files through the app), and
a basename match is a guess where an exact identifier exists.

Everything here is parsing, not decoding - no ffmpeg, no image decode.
That matters because the NAS has no video decoder at all.
"""

import re
import struct
from pathlib import Path

# Apple's MakerNote tag holding the Live Photo pairing UUID.
_APPLE_CONTENT_ID_TAG = 0x0011
_APPLE_MAKERNOTE_MAGIC = b"Apple iOS\x00"

# How far into a file to look for the markers. Both formats put their
# metadata near the front, and reading whole 4MB files just to find a
# header would make indexing far slower than it needs to be.
_HEAD_BYTES = 256 * 1024

_UUID_RE = re.compile(
    rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)


def _read_head(path, n=_HEAD_BYTES) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


# ---------------------------------------------------------------------------
# Apple
# ---------------------------------------------------------------------------

def apple_content_id(path) -> str | None:
    """
    The Live Photo pairing UUID from an Apple still (HEIC/JPG), or None.

    Parses the MakerNote by hand rather than via Pillow, which does not
    expose it, or exiftool, which is not installed here and would not be
    on the NAS either.

    The one trap: **MakerNote value offsets are relative to the start of
    the MakerNote block**, not to the TIFF header inside it. Using the
    TIFF header lands 10 bytes late and yields a truncated string that
    still looks plausibly UUID-shaped - verified against a real file
    where the correct base is the 'Apple iOS\\0' magic itself.
    """
    d = _read_head(path)
    base = d.find(_APPLE_MAKERNOTE_MAGIC)
    if base < 0:
        return None

    mm = d.find(b"MM", base, base + 32)
    if mm < 0:
        return None
    # Apple truncates the TIFF header: 'MM' then a 2-byte version, with
    # the IFD immediately after - NOT the standard 8-byte header with a
    # first-IFD pointer. Reading it as standard gives an entry count of 0
    # and silently finds nothing.
    ifd = mm + 2

    try:
        count = struct.unpack(">H", d[ifd:ifd + 2])[0]
    except struct.error:
        return None
    if not 0 < count < 256:
        return None

    for i in range(count):
        e = ifd + 2 + i * 12
        try:
            tag, typ, n = struct.unpack(">HHI", d[e:e + 8])
            off = struct.unpack(">I", d[e + 8:e + 12])[0]
        except struct.error:
            return None
        if tag != _APPLE_CONTENT_ID_TAG or typ != 2:
            continue
        raw = d[base + off:base + off + n]
        m = _UUID_RE.search(raw)
        return m.group(0).decode("ascii") if m else None
    return None


def quicktime_content_id(path) -> str | None:
    """
    The same pairing UUID from the .MOV half of a Live Photo, or None.

    QuickTime separates metadata keys from values, so the UUID does not
    sit next to the key name. Rather than walk the whole ilst atom, this
    confirms the live-photo marker is present and then takes the single
    UUID present - real Live Photo MOVs carry exactly one.

    Unlike the stills, this cannot read only the head: QuickTime places
    its metadata wherever the writer chose, and on a real iPhone clip the
    marker sat 1.2MB into a 4MB file - 30% in, well past any sane header
    window. So this scans the whole file, which is why it is called only
    for .MOV/.MP4 siblings of a still, never for every file.
    """
    try:
        with open(path, "rb") as f:
            d = f.read()
    except OSError:
        return None
    if b"com.apple.quicktime.content.identifier" not in d:
        return None
    found = _UUID_RE.findall(d)
    if not found:
        return None
    # Deduplicate: the identifier is written once as a key and once as a
    # value in some files.
    uniq = {u.decode("ascii") for u in found}
    return uniq.pop() if len(uniq) == 1 else sorted(uniq)[0]


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

def google_motion_offset(path) -> int | None:
    """
    Byte offset where the embedded MP4 starts inside a Motion Photo, or
    None if this is not one.

    The XMP advertises the video's length, and the video is appended, so
    the offset is (file size - length). That is preferred over scanning
    for an 'ftyp' box, which can also appear inside an embedded thumbnail.
    Falls back to the scan when the XMP length is missing or nonsensical.
    """
    d = _read_head(path)
    if b"GCamera:MotionPhoto" not in d and b"MotionPhoto" not in d:
        return None

    try:
        size = Path(path).stat().st_size
    except OSError:
        return None

    # Item:Length for the video item - the last one wins, since the
    # primary still is item 0.
    lengths = [int(m) for m in re.findall(rb"Item:Length=\"(\d+)\"", d)]
    if not lengths:
        lengths = [int(m) for m in re.findall(rb"Length=\"(\d+)\"", d)]
    for length in reversed(lengths):
        if 0 < length < size:
            return size - length

    i = d.find(b"ftyp")
    return i - 4 if i > 4 else None


def is_google_motion_photo(path) -> bool:
    return google_motion_offset(path) is not None


def extract_google_motion(path) -> tuple[int, int] | None:
    """(offset, length) of the embedded MP4, for serving as a byte range."""
    off = google_motion_offset(path)
    if off is None:
        return None
    try:
        return off, Path(path).stat().st_size - off
    except OSError:
        return None
