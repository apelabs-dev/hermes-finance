"""Draw a QR code (ISO/IEC 18004, model 2) as an SVG, with nothing but the standard library.

The dashboard shows the `otpauth://` URI of a new sign-in secret this way so an authenticator app can scan it. The
secret must never leave the machine, which is why the code is drawn here and not by a web service or a script.

Byte mode only (the UTF-8 bytes of the text), error correction L / M / Q / H, the smallest version (1-40) the data
fits in, and of the 8 data masks the one with the lowest ISO penalty score.
"""

from __future__ import annotations

import html
from itertools import groupby

LEVELS = ("L", "M", "Q", "H")
_LEVEL_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}   # the two level bits of the format information

# Versions 1..40: EC codewords per block, and the number of blocks, per level. With the codeword count of the version
# they give the whole ISO block table: the data is split as evenly as it goes, the long blocks one data byte longer.
_EC_PER_BLOCK = dict(zip(LEVELS, (
    (7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
     28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
     26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30,
     28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28,
     30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)))
_BLOCKS = dict(zip(LEVELS, (
    (1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
     8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
     17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20,
     23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25,
     25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)))

# The data masks: a module (row r, column c) outside the function patterns flips where the rule is true.
_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: r * c % 2 + r * c % 3 == 0,
    lambda r, c: (r * c % 2 + r * c % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + r * c % 3) % 2 == 0,
)


def _gf_tables() -> tuple[list[int], list[int]]:
    """Powers and logarithms of GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D)."""
    exp, log = [0] * 510, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = exp[i + 255] = x   # doubled, so exp[log a + log b] needs no modulo
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    return exp, log


_EXP, _LOG = _gf_tables()


def _mul(a: int, b: int) -> int:
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _rs_generator(degree: int) -> list[int]:
    """(x - a^0)(x - a^1)...(x - a^(degree-1)), highest power first, without its leading 1."""
    g = [1]
    for i in range(degree):
        g = [hi ^ _mul(lo, _EXP[i]) for hi, lo in zip(g + [0], [0] + g)]
    return g[1:]


def _rs_remainder(data: list[int], generator: list[int]) -> list[int]:
    """The EC codewords: data * x^degree mod generator (polynomial long division, one byte at a time)."""
    rem = [0] * len(generator)
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            rem = [r ^ _mul(g, factor) for r, g in zip(rem, generator)]
    return rem


def _raw_modules(version: int) -> int:
    """Modules left for data and EC codewords: all but the function patterns and the format / version information."""
    n = (16 * version + 128) * version + 64
    if version >= 2:
        k = version // 7 + 2   # alignment patterns per side
        n -= (25 * k - 10) * k - 55
        if version >= 7:
            n -= 36
    return n


def _data_codewords(version: int, ecc: str) -> int:
    return _raw_modules(version) // 8 - _EC_PER_BLOCK[ecc][version - 1] * _BLOCKS[ecc][version - 1]


def _capacity(version: int, ecc: str = "M") -> int:
    """The most bytes a version holds at a level: its data codewords less the mode (4 bits) and length headers."""
    return (_data_codewords(version, ecc) * 8 - 4 - (8 if version < 10 else 16)) // 8


def _codewords(data: bytes, version: int, ecc: str) -> list[int]:
    """The final codeword sequence: header + data + terminator + padding, split into blocks, EC added, interleaved."""
    count_bits = 8 if version < 10 else 16
    room = _data_codewords(version, ecc) * 8
    bits = "0100" + format(len(data), f"0{count_bits}b") + "".join(f"{b:08b}" for b in data)
    bits += "0" * min(4, room - len(bits))   # the terminator, cut short when the symbol is full
    bits += "0" * (-len(bits) % 8)
    stream = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    stream += [0xEC, 0x11] * ((room // 8 - len(stream)) // 2 + 1)
    stream = stream[:room // 8]

    ec_len, blocks = _EC_PER_BLOCK[ecc][version - 1], _BLOCKS[ecc][version - 1]
    total = _raw_modules(version) // 8
    short = blocks - total % blocks            # this many blocks are one data byte shorter than the rest
    short_len = total // blocks - ec_len
    generator = _rs_generator(ec_len)
    data_blocks, pos = [], 0
    for i in range(blocks):
        size = short_len + (i >= short)
        data_blocks.append(stream[pos:pos + size])
        pos += size
    ec_blocks = [_rs_remainder(b, generator) for b in data_blocks]
    out = [b[i] for i in range(short_len + 1) for b in data_blocks if i < len(b)]
    return out + [b[i] for i in range(ec_len) for b in ec_blocks]


def _alignment_positions(version: int) -> list[int]:
    """Row / column centres of the alignment patterns: 6, then evenly spaced (an even step) up to size - 7."""
    if version == 1:
        return []
    k = version // 7 + 2
    step = (version * 8 + k * 3 + 5) // (k * 4 - 4) * 2
    return [6] + [17 + 4 * version - 7 - i * step for i in range(k - 2, -1, -1)]


def _format_bits(ecc: str, mask: int) -> int:
    """Level and mask (5 bits), their BCH(15,5) check bits, XOR 0x5412 so the 15 bits are never all light."""
    data = _LEVEL_BITS[ecc] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    """The version (6 bits) and its BCH(18,6) check bits, for versions 7 and up."""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return version << 12 | rem


def _function_patterns(version: int) -> tuple[list[list[bool]], list[list[bool]]]:
    """The fixed part of a symbol: its modules, and which of them are function modules (never data, never masked)."""
    n = 17 + 4 * version
    dark = [[False] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(r: int, c: int, value: bool) -> None:
        dark[r][c], fixed[r][c] = value, True

    for i in range(n):   # timing patterns first: the finders and their separators overwrite the ends
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                if 0 <= r0 + dr < n and 0 <= c0 + dc < n:
                    ring = max(abs(dr - 3), abs(dc - 3))   # 0-1 centre, 2 light, 3 dark edge, 4 the separator
                    put(r0 + dr, c0 + dc, ring not in (2, 4))
    centres = _alignment_positions(version)
    corners = {(centres[0], centres[-1]), (centres[-1], centres[0]), (centres[0], centres[0])} if centres else set()
    for r in centres:
        for c in centres:
            if (r, c) not in corners:   # those three sit on the finders
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        put(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
    for i in range(9):   # room for the format information, drawn per mask later
        fixed[8][i] = fixed[i][8] = True
    for i in range(8):
        fixed[8][n - 1 - i] = fixed[n - 1 - i][8] = True
    dark[n - 8][8] = True   # the dark module, always there
    if version >= 7:
        bits = _version_bits(version)
        for i in range(18):   # two 6x3 copies, above the bottom-left finder and left of the top-right one
            bit = bool(bits >> i & 1)
            put(i // 3, n - 11 + i % 3, bit)
            put(n - 11 + i % 3, i // 3, bit)
    return dark, fixed


def _place(dark: list[list[bool]], fixed: list[list[bool]], codewords: list[int]) -> None:
    """Fill the data modules in the ISO zigzag: two-module columns from the right, up then down, skipping column 6.
    Remainder bits past the last codeword stay light (0) before masking."""
    n = len(dark)
    bits = [bool(b >> (7 - i) & 1) for b in codewords for i in range(8)]
    k = 0
    for right in range(n - 1, 0, -2):
        if right <= 6:
            right -= 1   # the vertical timing pattern takes column 6 out of the zigzag
        upward = (right + 1) & 2 == 0
        for vert in range(n):
            r = n - 1 - vert if upward else vert
            for c in (right, right - 1):
                if not fixed[r][c] and k < len(bits):
                    dark[r][c] = bits[k]
                    k += 1


def _masked(dark: list[list[bool]], fixed: list[list[bool]], ecc: str, mask: int) -> list[list[bool]]:
    """A copy with the mask applied to the data modules and the format information for (ecc, mask) drawn."""
    rule = _MASKS[mask]
    m = [[d != (not f and rule(r, c)) for c, (d, f) in enumerate(zip(drow, frow))]
         for r, (drow, frow) in enumerate(zip(dark, fixed))]
    n = len(m)
    bits = _format_bits(ecc, mask)
    for i in range(15):
        bit = bool(bits >> i & 1)
        # copy 1 around the top-left finder: bits 0-7 down column 8 (hopping the timing row), 8-14 leftward on row 8
        if i < 8:
            m[i + (i >= 6)][8] = bit
        else:
            m[8][15 - i - (i >= 9)] = bit
        # copy 2: bits 0-7 on row 8 under the top-right finder, 8-14 down column 8 right of the bottom-left finder
        if i < 8:
            m[8][n - 1 - i] = bit
        else:
            m[n - 15 + i][8] = bit
    return m


def _penalty(m: list[list[bool]]) -> int:
    """The ISO penalty score N1 + N2 + N3 + N4; the mask with the lowest one is used."""
    n = len(m)
    rows = ["".join("1" if v else "0" for v in row) for row in m]
    lines = rows + ["".join(col) for col in zip(*rows)]
    score = 0
    for line in lines:
        # N1: a run of 5+ modules of one colour scores 3, plus 1 per module past 5
        score += sum(k - 2 for k in (sum(1 for _ in run) for _, run in groupby(line)) if k >= 5)
        # N3: a 1:1:3:1:1 finder look-alike with 4 light modules on one side (outside the symbol counts as light)
        padded = "0000" + line + "0000"
        score += 40 * (padded.count("00001011101") + padded.count("10111010000"))
    # N2: every 2x2 block of one colour scores 3 (overlapping blocks all count)
    for a, b in zip(rows, rows[1:]):
        score += 3 * sum(1 for j in range(n - 1) if a[j] == a[j + 1] == b[j] == b[j + 1])
    # N4: 10 per full 5% the dark share is away from 50%
    dark = sum(row.count("1") for row in rows)
    return score + 10 * (abs(dark * 20 - n * n * 10) // (n * n))


def matrix(data: str | bytes, ecc: str = "M") -> list[list[bool]]:
    """The QR code modules of `data` (text goes in as UTF-8), True = dark, without the quiet zone."""
    if ecc not in _LEVEL_BITS:
        raise ValueError(f"error correction level must be one of L, M, Q, H, not {ecc!r}")
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    version = next((v for v in range(1, 41) if len(raw) <= _capacity(v, ecc)), 0)
    if not version:
        raise ValueError(f"{len(raw)} bytes don't fit in a QR code at level {ecc} (at most {_capacity(40, ecc)})")
    dark, fixed = _function_patterns(version)
    _place(dark, fixed, _codewords(raw, version, ecc))
    best = min(range(8), key=lambda mask: _penalty(_masked(dark, fixed, ecc, mask)))
    return _masked(dark, fixed, ecc, best)


def svg(data: str | bytes, ecc: str = "M", border: int = 4, label: str = "QR code") -> str:
    """A standalone SVG of the QR code with a `border`-module quiet zone; the page sizes it with CSS."""
    if border < 0:
        raise ValueError("border can't be negative")
    m = matrix(data, ecc)
    size = len(m) + 2 * border
    runs = []
    for r, row in enumerate(m):
        c = 0
        for is_dark, group in groupby(row):
            k = sum(1 for _ in group)
            if is_dark:   # one closed 1-module-high rectangle per horizontal run of dark modules
                runs.append(f"M{c + border} {r + border}h{k}v1h-{k}z")
            c += k
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges"'
            f' role="img" aria-label="{html.escape(label)}">'
            f'<rect width="{size}" height="{size}" fill="#fff"/>'
            f'<path fill="#000" d="{"".join(runs)}"/></svg>')
