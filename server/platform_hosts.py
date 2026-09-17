#!/usr/bin/env python3
"""
Host-specific workarounds, kept out of the core indexer.

Everything here exists because of one particular NAS, not because of
anything intrinsic to the app. The rule for this file: core code asks a
generic question ("is there an existing poster frame for this video?")
and the answer happens to be host-specific. Porting to another system
means editing this file and nothing else.

Currently only Synology DSM needs anything:

  DSM's bundled ffmpeg (4.1.9 on a DS124) has NO video decoders - no
  hevc, no h264 - and the RTD1619B exposes no /dev/dri. Extracting a
  frame from a video is therefore impossible on that hardware. But DSM's
  own Image Assistant has already decoded every video and left a poster
  frame beside it, so we can use that instead of decoding anything.

Detection is by looking for the artifact, never by sniffing the
platform: a library rsynced off a Synology onto a Linux box still has
its @eaDir posters and should still use them, and a Synology share with
Image Assistant switched off correctly falls back to ffmpeg. Asking
"does this file exist?" is right in both cases; asking "is this DSM?"
is wrong in both.
"""

from pathlib import Path

# DSM's Image Assistant writes these next to each media file, under
# <dir>/@eaDir/<filename>/. XL is 1280px with rotation already applied;
# the smaller sizes are too small to be worth preferring over a real
# decode when one is possible.
_DSM_SIDECAR = "@eaDir"
_DSM_POSTER = "SYNOPHOTO_THUMB_XL.jpg"


def existing_video_poster(src) -> Path | None:
    """
    An already-decoded still for this video, if the host left one lying
    around. Returns None when there is nothing usable, in which case the
    caller decodes a frame itself.

    Zero-length files are rejected here rather than by the caller: DSM
    writes empty *.fail markers when its own generation failed, and those
    would otherwise look like a valid poster right up until Pillow raised.
    """
    src = Path(src)
    poster = src.parent / _DSM_SIDECAR / src.name / _DSM_POSTER
    try:
        if poster.is_file() and poster.stat().st_size > 0:
            return poster
    except OSError:
        pass
    return None


# Below this, treat the poster as unusably soft rather than trust it -
# guards against a future DSM version changing its own thumbnail sizing
# and silently handing out a lower-quality preview than PREVIEW_MAX
# (1400) calls for. XL is documented at 1280px; anything meaningfully
# under that is not what this function is named for.
_MIN_USABLE_POSTER_EDGE = 1000


def existing_photo_thumb(src) -> Path | None:
    """
    An already-HEIC-decoded still for this photo, if DSM's Image Assistant
    left one lying around - same artifact existing_video_poster borrows for
    videos, used here to skip the indexer's own (subprocess-isolated,
    ~235MB peak measured on a real 5712x4284 HEIC) full decode entirely.

    Unlike existing_video_poster, the caller can always decode this file
    itself if it wants to - this is a memory/time optimisation, not a
    capability gap being worked around, so a poster that fails the size
    check is simply skipped rather than treated as the only option.
    """
    src = Path(src)
    poster = src.parent / _DSM_SIDECAR / src.name / _DSM_POSTER
    try:
        if not poster.is_file() or poster.stat().st_size == 0:
            return None
        from PIL import Image
        with Image.open(poster) as im:
            if min(im.size) < _MIN_USABLE_POSTER_EDGE:
                return None
    except OSError:
        return None
    return poster


_DSM_FILM = "SYNOPHOTO_FILM_H.mp4"


def existing_compat_video(src) -> Path | None:
    """
    An H.264 transcode of this video, if the host already made one.

    DSM's Image Assistant writes SYNOPHOTO_FILM_H.mp4 beside each video.
    It is lower resolution than the original (720x960 against 1920x1440 on
    the sampled clip) so it is a FALLBACK, never the default: the point is
    playability in browsers that cannot decode HEVC, not quality.

    Coverage is not total - 1,663 of 1,678 on the real library - so
    callers must handle None even on a host that generally provides these.
    """
    src = Path(src)
    film = src.parent / _DSM_SIDECAR / src.name / _DSM_FILM
    try:
        if film.is_file() and film.stat().st_size > 0:
            return film
    except OSError:
        pass
    return None


# Directory names that are host bookkeeping rather than library content.
# iter_media_files skips these so the indexer never walks into another
# tool's derivative cache - without it the walk is quadratic and full of
# files that only look like photos.
IGNORED_DIRNAMES = frozenset({_DSM_SIDECAR})
