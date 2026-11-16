"""Minimal QR code encoder (ISO/IEC 18004), standard library only.

Used to show the authenticator-app setup link as a QR code directly in the
browser, without any third-party service or dependency.  Supports byte mode
(UTF-8), versions 1..15, error correction levels L/M/Q/H, all eight masks
with the standard penalty rules, and renders the result as a compact SVG.
"""

from __future__ import annotations

# (EC codewords per block, blocks in group 1, data codewords per block in
# group 1, blocks in group 2, data codewords per block in group 2), versions 1..15.
_BLOCKS: dict[str, list[tuple[int, int, int, int, int]]] = {
    "L": [(7, 1, 19, 0, 0), (10, 1, 34, 0, 0), (15, 1, 55, 0, 0), (20, 1, 80, 0, 0),
          (26, 1, 108, 0, 0), (18, 2, 68, 0, 0), (20, 2, 78, 0, 0), (24, 2, 97, 0, 0),
          (30, 2, 116, 0, 0), (18, 2, 68, 2, 69), (20, 4, 81, 0, 0), (24, 2, 92, 2, 93),
          (26, 4, 107, 0, 0), (30, 3, 115, 1, 116), (22, 5, 87, 1, 88)],
    "M": [(10, 1, 16, 0, 0), (16, 1, 28, 0, 0), (26, 1, 44, 0, 0), (18, 2, 32, 0, 0),
          (24, 2, 43, 0, 0), (16, 4, 27, 0, 0), (18, 4, 31, 0, 0), (22, 2, 38, 2, 39),
          (22, 3, 36, 2, 37), (26, 4, 43, 1, 44), (30, 1, 50, 4, 51), (22, 6, 36, 2, 37),
          (22, 8, 37, 1, 38), (24, 4, 40, 5, 41), (24, 5, 41, 5, 42)],
    "Q": [(13, 1, 13, 0, 0), (22, 1, 22, 0, 0), (18, 2, 17, 0, 0), (26, 2, 24, 0, 0),
          (18, 2, 15, 2, 16), (24, 4, 19, 0, 0), (18, 2, 14, 4, 15), (22, 4, 18, 2, 19),
          (20, 4, 16, 4, 17), (24, 6, 19, 2, 20), (28, 4, 22, 4, 23), (26, 4, 20, 6, 21),
          (24, 8, 20, 4, 21), (20, 11, 16, 5, 17), (30, 5, 24, 7, 25)],
    "H": [(17, 1, 9, 0, 0), (28, 1, 16, 0, 0), (22, 2, 13, 0, 0), (16, 4, 9, 0, 0),
          (22, 2, 11, 2, 12), (28, 4, 15, 0, 0), (26, 4, 13, 1, 14), (26, 4, 14, 2, 15),
          (24, 4, 12, 4, 13), (28, 6, 15, 2, 16), (24, 3, 12, 8, 13), (28, 7, 14, 4, 15),
          (22, 12, 11, 4, 12), (24, 11, 12, 5, 13), (24, 11, 12, 7, 13)],
}
_ECC_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}
_ALIGN = [[6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42],
          [6, 26, 46], [6, 28, 50], [6, 30, 54], [6, 32, 58], [6, 34, 62],
          [6, 26, 46, 66], [6, 26, 48, 70]]  # index = version - 2 (v1 has none)
MAX_VERSION = 15
_FINDER_LIKE = bytes((1, 0, 1, 1, 1, 0, 1))

# GF(256) with primitive polynomial 0x11d.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _rs_remainder(data: list[int], n_ecc: int) -> list[int]:
    """Reed-Solomon remainder of `data` for a generator with `n_ecc` roots."""
    gen = [1]
    for i in range(n_ecc):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            if g:
                nxt[j + 1] ^= _EXP[_LOG[g] + i]
        gen = nxt
    rem = [0] * n_ecc
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            lf = _LOG[factor]
            for j in range(n_ecc):
                if gen[j + 1]:
                    rem[j] ^= _EXP[_LOG[gen[j + 1]] + lf]
    return rem


def _capacity(version: int, ecc: str) -> int:
    _, n1, d1, n2, d2 = _BLOCKS[ecc][version - 1]
    return n1 * d1 + n2 * d2


def _codewords(payload: bytes, version: int, ecc: str) -> list[int]:
    """Build data + EC codewords, interleaved over the blocks."""
    n_ecc, n1, d1, n2, d2 = _BLOCKS[ecc][version - 1]
    cap = n1 * d1 + n2 * d2
    count_bits = 8 if version < 10 else 16
    bits = f"0100{len(payload):0{count_bits}b}" + "".join(f"{b:08b}" for b in payload)
    bits += "0" * min(4, cap * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = 0
    while len(data) < cap:
        data.append((0xEC, 0x11)[pad])
        pad ^= 1
    blocks, pos = [], 0
    for size in [d1] * n1 + [d2] * n2:
        blocks.append(data[pos:pos + size])
        pos += size
    ecs = [_rs_remainder(b, n_ecc) for b in blocks]
    out = [b[i] for i in range(max(d1, d2)) for b in blocks if i < len(b)]
    out += [e[i] for i in range(n_ecc) for e in ecs]
    return out


def _bch(value: int, poly: int, check_bits: int) -> int:
    """Append BCH check bits to `value` using generator `poly`."""
    rem = value << check_bits
    top = poly.bit_length() - 1
    for i in range(rem.bit_length() - 1, top - 1, -1):
        if rem >> i & 1:
            rem ^= poly << (i - top)
    return value << check_bits | rem


def _build(version: int, ecc: str, words: list[int]) -> tuple[list[list[bool]], list[list[bool]]]:
    """Return (modules, is_function) with function patterns and data placed (no mask)."""
    size = version * 4 + 17
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def put(x: int, y: int, dark: bool) -> None:
        mod[y][x] = dark
        fn[y][x] = True

    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finder + separator
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    for i in range(size):  # timing
        if not fn[6][i]:
            put(i, 6, i % 2 == 0)
        if not fn[i][6]:
            put(6, i, i % 2 == 0)
    if version >= 2:  # alignment
        pos = _ALIGN[version - 2]
        for cy in pos:
            for cx in pos:
                if (cx, cy) in ((6, 6), (6, pos[-1]), (pos[-1], 6)):
                    continue  # overlaps a finder
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    _info(mod, fn, version, ecc, None)  # reserve the format / version areas (light)
    bits = [w >> (7 - i) & 1 for w in words for i in range(8)]
    k, x, upward = 0, size - 1, True
    while x > 0:
        if x == 6:
            x -= 1
        for step in range(size):
            y = size - 1 - step if upward else step
            for xx in (x, x - 1):
                if not fn[y][xx]:
                    mod[y][xx] = bool(bits[k]) if k < len(bits) else False
                    k += 1
        upward = not upward
        x -= 2
    return mod, fn


def _info(mod: list[list[bool]], fn: list[list[bool]], version: int, ecc: str,
          mask: int | None) -> None:
    """Write version info, format info and the dark module (all light if mask is None).

    Masks are scored before this information is written, with its areas light.
    """
    size = len(mod)
    ver = _bch(version, 0x1F25, 12) if version >= 7 and mask is not None else 0
    for i in range(18 if version >= 7 else 0):
        a, b = size - 11 + i % 3, i // 3
        mod[b][a] = mod[a][b] = bool(ver >> i & 1)
        fn[b][a] = fn[a][b] = True
    info = 0 if mask is None else _bch((_ECC_BITS[ecc] << 3) | mask, 0x537, 10) ^ 0x5412
    mod[size - 8][8], fn[size - 8][8] = mask is not None, True

    def put(x: int, y: int, i: int) -> None:
        mod[y][x] = bool(info >> i & 1)
        fn[y][x] = True

    for i in range(6):
        put(8, i, i)
    put(8, 7, 6)
    put(8, 8, 7)
    put(7, 8, 8)
    for i in range(9, 15):
        put(14 - i, 8, i)
    for i in range(8):
        put(size - 1 - i, 8, i)
    for i in range(8, 15):
        put(8, size - 15 + i, i)


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _finder_like(seq: bytes) -> int:
    """Rule 3: 1:1:3:1:1 pattern with four light modules on either side.

    The area outside the symbol counts as light, so a pattern touching the
    edge is penalised too.
    """
    size, count, idx = len(seq), 0, seq.find(_FINDER_LIKE)
    while idx != -1:
        end = idx + 7
        if (idx in (0, size - 7) or not any(seq[max(idx - 4, 0):idx])
                or not any(seq[end:end + 4])):
            count += 40
        else:
            end = idx + 4  # not a match, resume at the next possible start
        idx = seq.find(_FINDER_LIKE, end)
    return count


def _penalty(m: list[list[bool]]) -> int:
    size = len(m)
    score = 0
    lines = [bytes(r) for r in m] + [bytes(m[y][x] for y in range(size)) for x in range(size)]
    for line in lines:  # rule 1 (runs) and rule 3 (finder-like patterns)
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        score += run - 2 if run >= 5 else 0
        score += _finder_like(line)
    for y in range(size - 1):  # rule 2 (2x2 blocks)
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(v for row in m for v in row)  # rule 4 (balance)
    score += abs(dark * 20 - size * size * 10) // (size * size) * 10
    return score


def encode(text: str, ecc: str = "M") -> list[list[bool]]:
    """Encode `text` (UTF-8 byte mode) and return the module matrix (True = dark)."""
    if ecc not in _BLOCKS:
        raise ValueError(f"unknown error correction level: {ecc!r}")
    payload = text.encode("utf-8")
    for version in range(1, MAX_VERSION + 1):
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(payload) <= 8 * _capacity(version, ecc):
            break
    else:
        raise ValueError("text does not fit in a version 15 QR code")
    base, fn = _build(version, ecc, _codewords(payload, version, ecc))
    size = len(base)
    best, best_score, best_mask = None, 0, 0
    for mask, cond in enumerate(_MASKS):
        m = [row[:] for row in base]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and cond(x, y):
                    m[y][x] = not m[y][x]
        score = _penalty(m)
        if best is None or score < best_score:
            best, best_score, best_mask = m, score, mask
    _info(best, fn, version, ecc, best_mask)
    return best


def to_svg(matrix: list[list[bool]], scale: int = 4, quiet: int = 4,
           dark: str = "currentColor", light: str | None = None) -> str:
    """Render a module matrix as a compact SVG string (one path, one run per dark stretch)."""
    n = len(matrix)
    total = n + 2 * quiet
    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                parts.append(f"M{start + quiet} {y + quiet}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    bg = (f'<rect width="{total}" height="{total}" fill="{light}"/>' if light else "")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
            f'width="{total * scale}" height="{total * scale}" '
            f'shape-rendering="crispEdges" role="img">{bg}'
            f'<path fill="{dark}" d="{"".join(parts)}"/></svg>')
