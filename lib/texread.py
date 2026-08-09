"""
texread.py -- reads a cooked Texture2D package back into pixels.

The mirror of mkpkg.build_texture: that writes a preview texture from BGRA
pixels, this recovers pixels from any cooked texture of the same family so a
Dresscode mod's previews and thumbnail can be extracted as PNG files during
conversion to paks.

Only the formats Dresscode previews actually use decode to pixels: raw BGRA
and the two block-compressed formats (DXT1/DXT5). Anything else still parses
-- dimensions and format come back, only the pixels are None -- and the
caller simply skips writing a picture. Extraction is a courtesy for humans;
the lossless round trip carries the original texture package itself.
"""

import struct

import cityhash
import tagged
from zen import ZenPackage

TEXTURE2D = cityhash.object_id("/Script/Engine", "Texture2D", 1)

# Bulk-data flags meaning the pixels sit right here in the package.
_INLINE = 0x40


def read_texture(data):
    """
    Parse a cooked Texture2D package.

    Returns dict(width, height, pixel_format, mip) with `mip` the raw bytes
    of the largest mip -- or None when the package holds no Texture2D or
    streams its pixels from a .ubulk we are not looking at.
    """
    z = ZenPackage(data)
    span = z.find_export_payload(TEXTURE2D)
    if span is None:
        return None
    start, end = span

    # Step over the tagged properties; the serializer then leaves four zero
    # bytes before the native texture data begins.
    r = tagged.Reader(data, start, z)
    tagged.read_properties(r, end)
    o = r.o + 4

    o += 4                                          # strip flags
    o += 4                                          # bCooked
    o += 8 + 8                                      # constant, skip offset
    width, height, _packed = struct.unpack_from("<iii", data, o)
    o += 12
    pf_len = struct.unpack_from("<i", data, o)[0]
    o += 4
    pixel_format = data[o:o + pf_len - 1].decode("ascii")
    o += pf_len
    o += 4                                          # first mip to serialize
    mip_count = struct.unpack_from("<i", data, o)[0]
    o += 4

    mip = None
    for k in range(mip_count):
        o += 4                                      # mip bCooked
        flags, _count, size = struct.unpack_from("<Iii", data, o)
        o += 12 + 8                                 # + stored offset
        if not flags & _INLINE:
            return dict(width=width, height=height,
                        pixel_format=pixel_format, mip=None)
        if k == 0:
            mip = data[o:o + size]
        o += size
        o += 12                                     # this mip's dimensions
    return dict(width=width, height=height, pixel_format=pixel_format,
                mip=mip)


def to_bgra(width, height, pixel_format, mip):
    """The mip as BGRA rows, or None for a format we do not decode."""
    if mip is None:
        return None
    if pixel_format == "PF_B8G8R8A8":
        return mip[:width * height * 4]
    if pixel_format == "PF_DXT1":
        return _bc_decode(width, height, mip, 8, _dxt1_block)
    if pixel_format == "PF_DXT5":
        return _bc_decode(width, height, mip, 16, _dxt5_block)
    return None


def extract(data):
    """(width, height, BGRA) straight from a texture package, or None --
    including for texture layouts this parser does not know. Extraction is
    a courtesy; a picture that cannot be read is simply not written."""
    try:
        tex = read_texture(data)
        if not tex:
            return None
        bgra = to_bgra(tex["width"], tex["height"], tex["pixel_format"],
                       tex["mip"])
    except Exception:
        return None
    if bgra is None:
        return None
    return tex["width"], tex["height"], bgra


# ---------------------------------------------------------------------------
# Block decompression. DXT tiles are 4x4 texels; two 565 endpoint colors are
# interpolated into a 4-entry palette, 2 bits per texel select from it.
# ---------------------------------------------------------------------------

def _c565(v):
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _bc_decode(width, height, mip, block_bytes, block_fn):
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    if len(mip) < bw * bh * block_bytes:
        return None
    out = bytearray(width * height * 4)
    pos = 0
    for by in range(bh):
        for bx in range(bw):
            texels = block_fn(mip, pos)
            pos += block_bytes
            for ty in range(4):
                y = by * 4 + ty
                if y >= height:
                    break
                row = (y * width + bx * 4) * 4
                for tx in range(4):
                    if bx * 4 + tx >= width:
                        break
                    r, g, b, a = texels[ty * 4 + tx]
                    o = row + tx * 4
                    out[o] = b
                    out[o + 1] = g
                    out[o + 2] = r
                    out[o + 3] = a
    return bytes(out)


def _dxt1_block(d, o):
    c0, c1, bits = struct.unpack_from("<HHI", d, o)
    p0, p1 = _c565(c0), _c565(c1)
    if c0 > c1:
        pal = [p0 + (255,), p1 + (255,),
               tuple((2 * a + b) // 3 for a, b in zip(p0, p1)) + (255,),
               tuple((a + 2 * b) // 3 for a, b in zip(p0, p1)) + (255,)]
    else:
        pal = [p0 + (255,), p1 + (255,),
               tuple((a + b) // 2 for a, b in zip(p0, p1)) + (255,),
               (0, 0, 0, 0)]
    return [pal[(bits >> (2 * i)) & 3] for i in range(16)]


def _dxt5_block(d, o):
    a0, a1 = d[o], d[o + 1]
    abits = int.from_bytes(d[o + 2:o + 8], "little")
    if a0 > a1:
        apal = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    else:
        apal = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] \
            + [0, 255]
    color = _dxt1_block(d, o + 8)
    return [color[i][:3] + (apal[(abits >> (3 * i)) & 7],) for i in range(16)]
