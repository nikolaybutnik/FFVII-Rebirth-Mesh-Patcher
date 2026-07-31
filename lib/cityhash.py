"""
cityhash.py -- the hash that ties an IoStore container together.

WHY THIS EXISTS
---------------
Packages do not refer to each other by name. They refer to each other by a
64-bit hash of a name, and that hash is CityHash64. Nothing in a container can
be RENAMED without recomputing it -- which is what rename.py needs and what
this module provides.

THREE HASHES, THREE DIFFERENT INPUTS
------------------------------------
The same function is fed three different things, and mixing them up produces a
container that looks perfectly valid and loads nothing:

  package_id(path)      lowercased UTF-16LE of the package path.
                        This is the .utoc chunk ID and the container header's
                        package list.

  object_id(path)       lowercased UTF-16LE of "PackageName/ExportName".
                        The separator is a SLASH, not the dot you see in an
                        asset path. This is an export's GlobalImportIndex, and
                        the value every other package puts in its import table.

  name_hash(text)       lowercased ASCII -- NOT UTF-16. The per-name entries in
                        a package's name-hash blob.

HOW WE KNOW IT IS RIGHT
-----------------------
Reproduced, exactly, from real containers rather than from a spec: every package
ID, every name hash and every export ID in two mods and a game pakchunk. See
tests. Re-run that check after touching this file -- a hash function fails
silently and only at load time.

The implementation is CityHash64 v1.1. Two things bite when porting it:

  * Python integers do not wrap, so every multiply and add needs an explicit
    mask before it is used in an XOR or a rotate.
  * The 17..32 byte case uses k2 in `Rotate(b + k2, 18)`. Some published
    versions of CityHash use k0 there; this build does not.
"""

import struct

M64 = (1 << 64) - 1
M62 = (1 << 62) - 1

k0 = 0xC3A5C85C97CB3127
k1 = 0xB492B66FBE98F273
k2 = 0x9AE16A3B2F90404F

# Tags in the top two bits of an FPackageObjectIndex.
TYPE_EXPORT = 0
TYPE_SCRIPT_IMPORT = 1
TYPE_PACKAGE_IMPORT = 2
NULL_INDEX = 0xFFFFFFFFFFFFFFFF

# Stored in the first 8 bytes of a name-hash blob, ahead of the per-name hashes.
NAME_HASH_ALGORITHM = 0xC1640000


def _u64(b, o=0):
    return struct.unpack_from("<Q", b, o)[0]


def _u32(b, o=0):
    return struct.unpack_from("<I", b, o)[0]


def _rot(v, s):
    return v if s == 0 else ((v >> s) | (v << (64 - s))) & M64


def _shiftmix(v):
    return (v ^ (v >> 47)) & M64


def _bswap(v):
    return int.from_bytes(v.to_bytes(8, "little"), "big")


def _mix16(lo, hi):
    """CityHash's Hash128to64 -- the two-argument HashLen16."""
    mul = 0x9DDFEA08EB382D69
    a = ((lo ^ hi) * mul) & M64
    a ^= a >> 47
    b = ((hi ^ a) * mul) & M64
    b ^= b >> 47
    return (b * mul) & M64


def _mix16m(u, v, mul):
    """The three-argument HashLen16, which takes its own multiplier."""
    a = ((u ^ v) * mul) & M64
    a ^= a >> 47
    b = ((v ^ a) * mul) & M64
    b ^= b >> 47
    return (b * mul) & M64


def _len0to16(s):
    n = len(s)
    if n >= 8:
        mul = (k2 + n * 2) & M64
        a = (_u64(s, 0) + k2) & M64
        b = _u64(s, n - 8)
        c = (_rot(b, 37) * mul + a) & M64
        d = ((_rot(a, 25) + b) * mul) & M64
        return _mix16m(c, d, mul)
    if n >= 4:
        mul = (k2 + n * 2) & M64
        return _mix16m((n + (_u32(s, 0) << 3)) & M64, _u32(s, n - 4), mul)
    if n > 0:
        y = (s[0] + (s[n >> 1] << 8)) & 0xFFFFFFFF
        z = (n + (s[n - 1] << 2)) & 0xFFFFFFFF
        return (_shiftmix(((y * k2) & M64) ^ ((z * k0) & M64)) * k2) & M64
    return k2


def _len17to32(s):
    n = len(s)
    mul = (k2 + n * 2) & M64
    a = (_u64(s, 0) * k1) & M64
    b = _u64(s, 8)
    c = (_u64(s, n - 8) * mul) & M64
    d = (_u64(s, n - 16) * k2) & M64
    return _mix16m((_rot((a + b) & M64, 43) + _rot(c, 30) + d) & M64,
                   (a + _rot((b + k2) & M64, 18) + c) & M64, mul)


def _weak32(s, o, a, b):
    w, x, y, z = _u64(s, o), _u64(s, o + 8), _u64(s, o + 16), _u64(s, o + 24)
    a = (a + w) & M64
    b = _rot((b + a + z) & M64, 21)
    c = a
    a = (a + x + y) & M64
    b = (b + _rot(a, 44)) & M64
    return (a + z) & M64, (b + c) & M64


def _len33to64(s):
    n = len(s)
    mul = (k2 + n * 2) & M64
    a = (_u64(s, 0) * k2) & M64
    b = _u64(s, 8)
    c = _u64(s, n - 24)
    d = _u64(s, n - 32)
    e = (_u64(s, 16) * k2) & M64
    f = (_u64(s, 24) * 9) & M64
    g = _u64(s, n - 8)
    h = (_u64(s, n - 16) * mul) & M64
    u = (_rot((a + g) & M64, 43) + (_rot(b, 30) + c) * 9) & M64
    v = ((((a + g) & M64) ^ d) + f + 1) & M64
    w = (_bswap(((u + v) * mul) & M64) + h) & M64
    x = (_rot((e + f) & M64, 42) + c) & M64
    y = ((_bswap(((v + w) * mul) & M64) + g) * mul) & M64
    z = (e + f + c) & M64
    a = (_bswap((((x + z) * mul) + y) & M64) + b) & M64
    b = (_shiftmix((((z + a) * mul) + d + h) & M64) * mul) & M64
    return (b + x) & M64


def cityhash64(s):
    n = len(s)
    if n <= 16:
        return _len0to16(s)
    if n <= 32:
        return _len17to32(s)
    if n <= 64:
        return _len33to64(s)

    x = _u64(s, n - 40)
    y = (_u64(s, n - 16) + _u64(s, n - 56)) & M64
    z = _mix16((_u64(s, n - 48) + n) & M64, _u64(s, n - 24))
    v = _weak32(s, n - 64, n & M64, z)
    w = _weak32(s, n - 32, (y + k1) & M64, x)
    x = (x * k1 + _u64(s, 0)) & M64

    # Whole 64-byte blocks only; the tail was folded in above.
    remaining = (n - 1) & ~63
    o = 0
    while True:
        x = (_rot((x + y + v[0] + _u64(s, o + 8)) & M64, 37) * k1) & M64
        y = (_rot((y + v[1] + _u64(s, o + 48)) & M64, 42) * k1) & M64
        x ^= w[1]
        y = (y + v[0] + _u64(s, o + 40)) & M64
        z = (_rot((z + w[0]) & M64, 33) * k1) & M64
        v = _weak32(s, o, (v[1] * k1) & M64, (x + w[0]) & M64)
        w = _weak32(s, o + 32, (z + w[1]) & M64, (y + _u64(s, o + 16)) & M64)
        z, x = x, z
        o += 64
        remaining -= 64
        if remaining == 0:
            break

    return _mix16((_mix16(v[0], w[0]) + (_shiftmix(y) * k1) + z) & M64,
                  (_mix16(v[1], w[1]) + x) & M64)


def package_id(package_name):
    """FPackageId for a package path, e.g. "/Game/Character/.../PC0002_00"."""
    return cityhash64(package_name.lower().encode("utf-16-le"))


def object_id(package_name, object_path, type_tag=TYPE_PACKAGE_IMPORT):
    """
    FPackageObjectIndex for one object inside a package.

    `object_path` is the export's name, or its outer chain joined with "/" for a
    subobject. Note the separator between package and object is also "/" -- an
    asset path's dot does not appear here.
    """
    full = f"{package_name}/{object_path}"
    return (type_tag << 62) | (cityhash64(full.lower().encode("utf-16-le")) & M62)


def name_hash(text):
    """
    One entry of a package's name-hash blob.

    Hashed over the name AS STORED, so the width matters: an ASCII name is one
    byte per character, a name needing anything else is two (UTF-16LE). Hashing
    everything as UTF-16 -- the rule for the other two IDs here -- silently
    corrupts every ASCII name, which is nearly all of them.
    """
    lowered = text.lower()
    if text.isascii():
        return cityhash64(lowered.encode("ascii"))
    return cityhash64(lowered.encode("utf-16-le"))
