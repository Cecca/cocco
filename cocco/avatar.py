"""Deterministic SVG identicon generator used as a fallback user avatar."""

import hashlib

GRID = 5
IDENTICON_MIME = "image/svg+xml"

# An avatar is shown at 32 pixels. This is generous for that and still small
# enough to keep in the row: `user.image` is a column, not a file in `Storage`.
MAX_AVATAR_BYTES = 512 << 10

# Magic bytes, in the order they are cheapest to test. The mime a picture is
# served back under comes from *here* and never from the upload's own
# `content_type`: that header is written by whoever is uploading.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_mime(data: bytes) -> str | None:
    """The mime type of ``data``, or None if it is not a picture we accept.

    Raster formats only, and SVG is missing on purpose even though the
    identicon below *is* one: `/users/{id}/avatar` serves the stored bytes back
    from this origin under the stored mime, so an uploaded SVG would be a
    script running as the site. We generate the identicons; we do not generate
    uploads.
    """
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # WEBP is a RIFF container: the tag sits after the four-byte length.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def generate_identicon(seed: str) -> tuple[bytes, str]:
    """Return (image_bytes, mime_type) for a 5x5 horizontally-mirrored identicon
    derived deterministically from ``seed``."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    color = f"#{digest[0]:02x}{digest[1]:02x}{digest[2]:02x}"

    half = (GRID + 1) // 2
    rects: list[str] = []
    for y in range(GRID):
        for x in range(half):
            if digest[3 + y * half + x] & 1:
                rects.append(f'<rect x="{x}" y="{y}" width="1" height="1"/>')
                mirror = GRID - 1 - x
                if mirror != x:
                    rects.append(f'<rect x="{mirror}" y="{y}" width="1" height="1"/>')

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{GRID}" height="{GRID}" fill="#f3f4f6"/>'
        f'<g fill="{color}">{"".join(rects)}</g>'
        f"</svg>"
    )
    return svg.encode("utf-8"), IDENTICON_MIME
