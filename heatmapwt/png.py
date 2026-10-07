"""Read and write PNGs with nothing but the standard library.

The project has no third-party dependencies, which keeps it to a Python install
and a Go toolchain on a new machine. Only what this needs is implemented:
8-bit, non-interlaced images on the way in, RGBA on the way out.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def read_png(path: Path) -> tuple[int, int, int, bytes]:
    """(width, height, channels, pixels) with one byte per channel.

    A palette image is expanded to RGB, so the caller never sees indices.
    """
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")

    pos, idat, palette = 8, b"", None
    width = height = depth = colour = interlace = 0
    while pos + 8 <= len(data):
        length, tag = struct.unpack(">I", data[pos:pos + 4])[0], data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"PLTE":
            palette = body
        elif tag == b"IEND":
            break
        pos += 12 + length

    if depth != 8 or interlace != 0:
        raise ValueError(f"{path}: only 8-bit non-interlaced PNGs are supported "
                         f"(got depth {depth}, interlace {interlace})")

    channels = CHANNELS[colour]
    stride = width * channels
    raw = zlib.decompress(idat)
    out = bytearray(height * stride)
    previous = bytearray(stride)
    at = 0
    for y in range(height):
        kind = raw[at]
        at += 1
        line = bytearray(raw[at:at + stride])
        at += stride
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 255
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                guess = a + b - c
                da, db, dc = abs(guess - a), abs(guess - b), abs(guess - c)
                line[i] = (line[i] + (a if (da <= db and da <= dc)
                                      else (b if db <= dc else c))) & 255
        elif kind != 0:
            raise ValueError(f"{path}: unknown row filter {kind}")
        out[y * stride:(y + 1) * stride] = line
        previous = line

    if colour == 3:
        if palette is None:
            raise ValueError(f"{path}: palette image without a PLTE chunk")
        rgb = bytearray(width * height * 3)
        for i in range(width * height):
            at = out[i] * 3
            rgb[i * 3:i * 3 + 3] = palette[at:at + 3]
        return width, height, 3, bytes(rgb)
    return width, height, channels, bytes(out)


def write_png(path: Path, width: int, height: int, pixels: list[bytes]) -> None:
    """Write an RGBA PNG from one bytes object per row."""
    raw = b"".join(b"\x00" + row for row in pixels)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"
                           + chunk(b"IHDR", header)
                           + chunk(b"IDAT", zlib.compress(raw, 9))
                           + chunk(b"IEND", b""))
