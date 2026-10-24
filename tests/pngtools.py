"""Tiny PNG read/write helpers for tests (stdlib zlib only): decode the app icons, and build logo fixtures."""

from __future__ import annotations

import struct
import zlib

SIG = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_png(width: int, height: int, pixel=lambda x, y: (255, 255, 255, 255)) -> bytes:
    """An 8-bit RGBA PNG; pixel(x, y) -> (r, g, b, a)."""
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))
    return (SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def logo_png(size: int = 64) -> bytes:
    """A small non-blank logo: a dark disc on white."""
    c = size / 2
    return make_png(size, size, lambda x, y: (20, 40, 160, 255) if (x - c) ** 2 + (y - c) ** 2 < (size / 3) ** 2
                    else (255, 255, 255, 255))


def decode_png(blob: bytes) -> tuple[int, int, int, list[bytes]]:
    """(width, height, channels, rows) for 8-bit, non-interlaced grey/RGB/grey+alpha/RGBA PNGs."""
    assert blob[:8] == SIG
    pos, idat, head = 8, b"", None
    while pos < len(blob):
        n = struct.unpack(">I", blob[pos:pos + 4])[0]
        kind, data = blob[pos + 4:pos + 8], blob[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            idat += data
    width, height, depth, ctype, _, _, interlace = head
    assert depth == 8 and interlace == 0 and ctype in CHANNELS, head
    ch = CHANNELS[ctype]
    stride = width * ch
    raw = zlib.decompress(idat)
    rows, prev = [], bytearray(stride)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return width, height, ch, rows


def pixel(decoded, x: int, y: int) -> tuple[int, ...]:
    _, _, ch, rows = decoded
    return tuple(rows[y][x * ch:(x + 1) * ch])
