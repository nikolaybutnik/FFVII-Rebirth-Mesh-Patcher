"""
pngfile.py -- PNG to and from the BGRA pixels a Texture2D mip wants.

Only PNG is accepted from users (JPEG has no standard-library decoder, and
shipping one is not worth it for preview pictures). All common 8-bit PNG
flavors are handled -- grayscale, palette, truecolor, each with or without
alpha, all five row filters. Interlaced and 16-bit files are rare enough to
refuse with advice rather than support.

encode() is the other direction: converting a Dresscode mod to loose paks
extracts its preview pictures so the person can see (and later swap) them.
"""

import struct
import zlib


def encode(width, height, bgra):
    """The pixels as a plain truecolor+alpha PNG (filter 0, one IDAT)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)                               # row filter: none
        row = bgra[y * width * 4:(y + 1) * width * 4]
        for i in range(0, len(row), 4):
            raw += bytes((row[i + 2], row[i + 1], row[i], row[i + 3]))

    def chunk(kind, body):
        c = kind + body
        return struct.pack(">I", len(body)) + c \
            + struct.pack(">I", zlib.crc32(c))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                         8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def decode(path):
    """(width, height, BGRA bytes) for the PNG at `path`."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"{path} is not a PNG file -- previews and "
                           "thumbnails must be .png")

    width = height = None
    bit_depth = color_type = interlace = None
    palette = b""
    trans = b""
    idat = []
    o = 8
    while o + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, o)
        body = data[o + 8:o + 8 + length]
        o += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trans = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break

    if width is None:
        raise RuntimeError(f"{path} has no PNG header")
    if interlace:
        raise RuntimeError(f"{path} is an interlaced PNG -- re-save it "
                           "without interlacing (any editor can)")
    if bit_depth != 8:
        raise RuntimeError(f"{path} uses {bit_depth}-bit channels -- "
                           "re-save it as a standard 8-bit PNG")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise RuntimeError(f"{path}: unsupported PNG color type {color_type}")

    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        raise RuntimeError(f"{path} is truncated")

    # Undo the row filters in place; prev is the reconstructed row above.
    rows = []
    prev = bytearray(stride)
    for y in range(height):
        base = y * (stride + 1)
        filt = raw[base]
        row = bytearray(raw[base + 1:base + 1 + stride])
        if filt == 1:                                   # Sub
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif filt == 2:                                 # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif filt == 3:                                 # Average
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filt == 4:                                 # Paeth
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pred) & 0xFF
        elif filt != 0:
            raise RuntimeError(f"{path}: unknown PNG filter {filt}")
        rows.append(row)
        prev = row

    out = bytearray(width * height * 4)
    pos = 0
    for row in rows:
        if color_type == 6:                             # RGBA
            for i in range(0, stride, 4):
                out[pos] = row[i + 2]
                out[pos + 1] = row[i + 1]
                out[pos + 2] = row[i]
                out[pos + 3] = row[i + 3]
                pos += 4
        elif color_type == 2:                           # RGB
            for i in range(0, stride, 3):
                out[pos] = row[i + 2]
                out[pos + 1] = row[i + 1]
                out[pos + 2] = row[i]
                out[pos + 3] = 255
                pos += 4
        elif color_type == 3:                           # palette
            for i in range(stride):
                p = row[i] * 3
                out[pos] = palette[p + 2]
                out[pos + 1] = palette[p + 1]
                out[pos + 2] = palette[p]
                out[pos + 3] = trans[row[i]] if row[i] < len(trans) else 255
                pos += 4
        elif color_type == 0:                           # grayscale
            for i in range(stride):
                g = row[i]
                out[pos] = out[pos + 1] = out[pos + 2] = g
                out[pos + 3] = 255
                pos += 4
        else:                                           # gray + alpha
            for i in range(0, stride, 2):
                g = row[i]
                out[pos] = out[pos + 1] = out[pos + 2] = g
                out[pos + 3] = row[i + 1]
                pos += 4
    return width, height, bytes(out)
