"""The QR encoder: golden symbols (each one read back exactly by OpenCV and Apple's Vision decoder when it was
recorded), the ISO structure of a symbol, version selection, and the SVG it's drawn as."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from itertools import groupby

import pytest

from hermes_finance import qr

OTP = ("otpauth://totp/Hermes%20Finances:owner?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
       "&issuer=Hermes%20Finances&algorithm=SHA1&digits=6&period=30")
UTF8 = "Hermès Finances – ключ 秘密 ✓ 🔐"
SVG_NS = "{http://www.w3.org/2000/svg}"

HELLO_M = """
111111101101001111111 100000100110101000001 101110100111101011101 101110101001001011101 101110101000101011101
100000101011001000001 111111101010101111111 000000001111100000000 100010111111011111001 000111001011100101111
101100101011001110010 111001000100011010000 001011100100111000110 000000001110111001011 111111101100110001010
100000100001100100010 101110101001001110101 101110100001100001011 101110100111001111000 100000100100011000000
111111101000111110101""".split()

# sha256 of the rows ("0"/"1", joined by newlines) and the version they come out at
GOLDEN = [
    (OTP, "M", 8, "d53b0ae404d9f688af316db984e0f766cfaeb462be8c86ac5640821a6d403350"),
    (OTP, "L", 7, "6e6db770f0421418667f92e18c8ce3ef42ce5fe43650b75816ba889a881e970b"),
    (UTF8, "Q", 4, "8da4680e4b8a0f068b13338e6cdd2c6c98e81896e6da81f2bac08c339dabad53"),
]

# ISO/IEC 18004 table 7: the most bytes each version holds in byte mode, per level L, M, Q, H
ISO_CAPACITY = {1: (17, 14, 11, 7), 7: (154, 122, 86, 64), 9: (230, 180, 130, 98), 10: (271, 213, 151, 119),
                40: (2953, 2331, 1663, 1273)}
# ISO/IEC 18004 annex E: alignment pattern centres
ISO_ALIGNMENT = {1: [], 2: [6, 18], 7: [6, 22, 38], 14: [6, 26, 46, 66], 32: [6, 34, 60, 86, 112, 138],
                 40: [6, 30, 58, 86, 114, 142, 170]}
LEVEL_OF = {1: "L", 0: "M", 3: "Q", 2: "H"}


def rows(m):
    return ["".join("1" if v else "0" for v in row) for row in m]


def gf2_mod(value: int, poly: int) -> int:
    """value mod poly, both polynomials over GF(2): a codeword of a BCH code leaves 0."""
    while value.bit_length() >= poly.bit_length():
        value ^= poly << (value.bit_length() - poly.bit_length())
    return value


def gf256_mul(a: int, b: int) -> int:
    """Carry-less multiply mod 0x11D, kept apart from the module's log tables."""
    out = 0
    while b:
        if b & 1:
            out ^= a
        a, b = a << 1 ^ (0x11D if a & 0x80 else 0), b >> 1
    return out


def syndromes(block: list[int], count: int) -> list[int]:
    """The block as a polynomial evaluated at a^0..a^(count-1): all 0 for a valid Reed-Solomon codeword."""
    out, alpha = [], 1
    for _ in range(count):
        acc = 0
        for byte in block:
            acc = gf256_mul(acc, alpha) ^ byte
        out.append(acc)
        alpha = gf256_mul(alpha, 2)
    return out


@lru_cache
def full(version: int, ecc: str = "L") -> list[list[bool]]:
    """A symbol of exactly this version: as many bytes as it holds."""
    return qr.matrix("a" * qr._capacity(version, ecc), ecc)


# --- golden symbols ---------------------------------------------------------------------------------------------

def test_golden_hello():
    assert rows(qr.matrix("HELLO")) == HELLO_M
    assert qr.matrix(b"HELLO", "M") == qr.matrix("HELLO", "M")


@pytest.mark.parametrize("data, ecc, version, digest", GOLDEN)
def test_golden(data, ecc, version, digest):
    m = qr.matrix(data, ecc)
    assert len(m) == 17 + 4 * version
    assert hashlib.sha256("\n".join(rows(m)).encode()).hexdigest() == digest
    assert qr.matrix(data.encode("utf-8"), ecc) == m


# --- structure --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("version", sorted(ISO_ALIGNMENT))
def test_function_patterns(version):
    m = full(version)
    n = len(m)
    assert n == 17 + 4 * version
    finder = [[max(abs(r - 3), abs(c - 3)) != 2 for c in range(7)] for r in range(7)]
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        assert [row[c0:c0 + 7] for row in m[r0:r0 + 7]] == finder
    for i in range(8):   # separators: light around the finders' inner edges
        assert not any((m[7][i], m[i][7], m[7][n - 1 - i], m[i][n - 8], m[n - 8][i], m[n - 1 - i][7]))
    for i in range(8, n - 8):   # timing patterns
        assert m[6][i] == m[i][6] == (i % 2 == 0)
    assert m[n - 8][8]   # the dark module
    centres = ISO_ALIGNMENT[version]
    assert qr._alignment_positions(version) == centres
    on_finders = {(centres[0], centres[0]), (centres[0], centres[-1]), (centres[-1], centres[0])} if centres else set()
    for r in centres:
        for c in centres:
            if (r, c) not in on_finders:
                block = [row[c - 2:c + 3] for row in m[r - 2:r + 3]]
                assert block == [[max(abs(dr), abs(dc)) != 1 for dc in range(-2, 3)] for dr in range(-2, 3)]


@pytest.mark.parametrize("ecc", qr.LEVELS)
@pytest.mark.parametrize("data", ["HELLO", OTP])
def test_format_information(data, ecc):
    m = qr.matrix(data, ecc)
    n = len(m)
    # both copies, most significant bit first (ISO figure 25)
    first = [(8, c) for c in (0, 1, 2, 3, 4, 5, 7, 8)] + [(r, 8) for r in (7, 5, 4, 3, 2, 1, 0)]
    second = [(r, 8) for r in range(n - 1, n - 8, -1)] + [(8, c) for c in range(n - 8, n)]
    copies = [int("".join("1" if m[r][c] else "0" for r, c in cells), 2) for cells in (first, second)]
    assert copies[0] == copies[1]
    word = copies[0] ^ 0x5412
    assert gf2_mod(word, 0x537) == 0   # BCH(15,5)
    assert LEVEL_OF[word >> 13] == ecc
    # the mask it names is the one applied, and no other mask scores a lower penalty
    mask, version = word >> 10 & 7, (n - 17) // 4
    dark, fixed = qr._function_patterns(version)
    qr._place(dark, fixed, qr._codewords(data.encode(), version, ecc))
    assert qr._masked(dark, fixed, ecc, mask) == m
    assert qr._penalty(m) == min(qr._penalty(qr._masked(dark, fixed, ecc, k)) for k in range(8))


def test_format_and_version_words_match_iso_examples():
    assert qr._format_bits("M", 5) == 0b100000011001110   # ISO annex C
    assert qr._version_bits(7) == 0x07C94                 # ISO annex D
    assert qr._version_bits(40) == 0x28C69


@pytest.mark.parametrize("version", [7, 14, 32, 40])
def test_version_information(version):
    m = full(version)
    n = len(m)
    bottom_left = sum(m[n - 11 + i % 3][i // 3] << i for i in range(18))
    top_right = sum(m[i // 3][n - 11 + i % 3] << i for i in range(18))
    assert bottom_left == top_right
    assert bottom_left >> 12 == version
    assert gf2_mod(bottom_left, 0x1F25) == 0   # BCH(18,6)


def test_reed_solomon_known_vectors():
    hello_world = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]   # "HELLO WORLD", 1-M
    assert qr._rs_remainder(hello_world, qr._rs_generator(10)) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
    iso = [16, 32, 12, 86, 97, 128, 236, 17, 236, 17, 236, 17, 236, 17, 236, 17]   # "01234567", 1-M (ISO annex I)
    assert qr._rs_remainder(iso, qr._rs_generator(10)) == [165, 36, 212, 193, 237, 54, 199, 135, 44, 85]


def test_blocks_are_interleaved():
    # 5-Q per ISO table 9: two blocks of 15 data codewords, two of 16, 18 EC codewords each
    data = bytes(range(56))
    stream = qr._codewords(data, 5, "Q")
    assert len(stream) == 134
    sizes = (15, 15, 16, 16)
    blocks = [[] for _ in sizes]
    it = iter(stream)
    for i in range(16):
        for b, size in zip(blocks, sizes):
            if i < size:
                b.append(next(it))
    for _ in range(18):   # then the EC codewords, interleaved the same way
        for b in blocks:
            b.append(next(it))
    assert next(it, None) is None
    joined = [byte for b in blocks for byte in b[:-18]]
    assert joined[:3] == [0x43, 0x80, 0x00]              # mode 0100, length 56, the bytes ...
    assert joined[-5:] == [0x70, 0xEC, 0x11, 0xEC, 0x11]  # ... byte 55 + 4 terminator bits, then 0xEC 0x11 padding
    for b in blocks:
        assert syndromes(b, 18) == [0] * 18


@pytest.mark.parametrize("version", sorted(ISO_CAPACITY))
@pytest.mark.parametrize("level", range(4))
def test_version_boundaries(version, level):
    ecc, most = qr.LEVELS[level], ISO_CAPACITY[version][level]
    assert len(qr.matrix(b"\xab" * most, ecc)) == 17 + 4 * version
    if version < 40:
        assert len(qr.matrix(b"\xab" * (most + 1), ecc)) == 17 + 4 * (version + 1)
    else:
        with pytest.raises(ValueError, match="don't fit"):
            qr.matrix(b"\xab" * (most + 1), ecc)


def test_smallest_version_is_picked():
    assert len(qr.matrix("")) == 21
    assert len(qr.matrix(OTP, "M")) == 17 + 4 * 8
    assert len(qr.matrix("é" * 7, "M")) == 21 and len(qr.matrix("é" * 8, "M")) == 25   # counted in UTF-8 bytes


def test_bad_arguments():
    for ecc in ("X", "m", "", "LM"):
        with pytest.raises(ValueError, match="L, M, Q, H"):
            qr.matrix("HELLO", ecc)
    with pytest.raises(ValueError):
        qr.svg("HELLO", border=-1)


def test_penalty_weights():
    light = [[False] * 5 for _ in range(5)]
    # N1: 10 lines of 5 = 10 x 3; N2: 16 2x2 blocks x 3; N3: none; N4: 0% dark is 10 steps of 5% off 50%
    assert qr._penalty(light) == 30 + 48 + 0 + 100
    assert qr._penalty([[(r + c) % 2 == 0 for c in range(4)] for r in range(4)]) == 0   # checkerboard, 50% dark
    # a 1:1:3:1:1 row (10111010000) above ten light rows: N1 90 (rows) + 94 (columns), N2 93 x 3,
    # N3 2 x 40 (4 light modules on both sides, the left ones outside the symbol), N4 5/121 dark = 9 steps
    finder_row = [[c in (0, 2, 3, 4, 6) for c in range(11)]] + [[False] * 11 for _ in range(10)]
    assert qr._penalty(finder_row) == 184 + 279 + 80 + 90


# --- svg --------------------------------------------------------------------------------------------------------

def cells_of(d: str) -> list[tuple[int, int, int]]:
    assert re.fullmatch(r"(?:M\d+ \d+h\d+v1h-\d+z)+", d)
    runs = [tuple(map(int, t)) for t in re.findall(r"M(\d+) (\d+)h(\d+)v1h-(\d+)z", d)]
    assert all(k == back for _, _, k, back in runs)
    return [(x, y, k) for x, y, k, _ in runs]


@pytest.mark.parametrize("border", [4, 0, 2])
def test_svg(border):
    label = 'Scan <me> & "go"'
    doc = qr.svg(OTP, "M", border=border, label=label)
    root = ET.fromstring(doc)
    m = qr.matrix(OTP, "M")
    size = len(m) + 2 * border
    assert root.tag == SVG_NS + "svg"
    assert [el.tag for el in root.iter()] == [SVG_NS + "svg", SVG_NS + "rect", SVG_NS + "path"]
    assert root.attrib == {"viewBox": f"0 0 {size} {size}", "shape-rendering": "crispEdges", "role": "img",
                           "aria-label": label}
    assert "&lt;me&gt; &amp; &quot;go&quot;" in doc
    rect, path = root
    assert rect.attrib == {"width": str(size), "height": str(size), "fill": "#fff"}
    assert set(path.attrib) == {"fill", "d"} and path.get("fill") == "#000"
    assert "script" not in doc and "href" not in doc and doc.count("http") == 1   # just the xmlns
    runs = cells_of(path.get("d"))
    cells = [(y - border, x - border + i) for x, y, k in runs for i in range(k)]
    dark = [(r, c) for r, row in enumerate(m) for c, v in enumerate(row) if v]
    assert sorted(cells) == dark   # every dark module exactly once, nothing else
    assert len(runs) == sum(1 for row in m for v, _ in groupby(row) if v)   # one square per horizontal run


def test_svg_default_label():
    assert 'aria-label="QR code"' in qr.svg("HELLO")
    assert qr.svg(b"HELLO") == qr.svg("HELLO")
