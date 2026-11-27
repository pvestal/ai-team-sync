"""Synthetic image transport probe with no filesystem or network inputs."""

import base64
import secrets
import struct
import zlib

from mcp.types import ImageContent, Tool, ToolAnnotations

TOOL = Tool(
    name="media_probe",
    description="Return a synthetic PNG containing six digits for visual transport verification.",
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)

_DIGITS = (
    (7, 5, 5, 5, 7), (2, 6, 2, 2, 7), (7, 1, 7, 4, 7), (7, 1, 7, 1, 7),
    (5, 5, 7, 1, 1), (7, 4, 7, 1, 7), (7, 4, 7, 5, 7), (7, 1, 1, 1, 1),
    (7, 5, 7, 5, 7), (7, 5, 7, 1, 7),
)
_SCALE = 12
_WIDTH, _HEIGHT = 25 * _SCALE, 7 * _SCALE


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack("!I", len(payload)) + kind + payload + struct.pack("!I", zlib.crc32(kind + payload))


def _png(code: str) -> bytes:
    raster = [bytearray([0, *([224 + y * 24 // _HEIGHT] * _WIDTH)]) for y in range(_HEIGHT)]
    for index, digit in enumerate(code):
        for glyph_y, bits in enumerate(_DIGITS[int(digit)]):
            for glyph_x in range(3):
                if bits & (4 >> glyph_x):
                    x = 1 + (1 + index * 4 + glyph_x) * _SCALE
                    y = (1 + glyph_y) * _SCALE
                    for row in raster[y:y + _SCALE]:
                        row[x:x + _SCALE] = bytes([22]) * _SCALE
    header = struct.pack("!2I5B", _WIDTH, _HEIGHT, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(b"".join(raster))) + _chunk(b"IEND", b""))


def probe() -> list[ImageContent]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    return [ImageContent(type="image", mimeType="image/png", data=base64.b64encode(_png(code)).decode("ascii"))]
