"""Deterministic SVG identicon generator used as a fallback user avatar."""

import hashlib

GRID = 5
IDENTICON_MIME = "image/svg+xml"


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
