"""
assetreg.py -- writes the AssetRegistry.bin a Dresscode plugin pak carries.

The registry is how the mod loader DISCOVERS a plugin's assets: FF7RML scans
it for the registration data assets, and Dresscode resolves previews and
meshes through it. A plugin pak without one is invisible.

The format is UE4.26's runtime FAssetRegistryState, version 7: a header, one
block per asset (object path, package path, class, package name, asset name,
tag strings), an empty dependency section, and an FName table at the end --
each name with two 16-bit preload hashes whose algorithms were identified by
matching real registries (104/104 names): FCrc::Strihash_DEPRECATED and
FCrc::StrCrc32.
"""

import struct
import zlib

VERSION_GUID = bytes.fromhex("e79e7f713a49b0e93291b3880781381b")
VERSION = 7

# After the assets: an empty dependency/package-data section, identical in
# every registry seen. Copied, not interpreted.
TAIL = bytes.fromhex("04000000000000000000000000000000")

# FCrc::Strihash_DEPRECATED uses the FORWARD table for 0x04C11DB7 but walks
# it with right shifts -- mathematically wrong, kept stable by Epic because
# hashes were saved to disk. Which is exactly why it matters here.
_FWD = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7 if _c & 0x80000000 else _c << 1) \
            & 0xFFFFFFFF
    _FWD.append(_c)


def _strihash(text):
    h = 0
    for ch in text.upper():
        h = ((h >> 8) & 0x00FFFFFF) ^ _FWD[(h ^ ord(ch)) & 0xFF]
    return h


def _strcrc32(text):
    return zlib.crc32(text.encode("utf-32-le")) & 0xFFFFFFFF


def split_name(text):
    """FName number splitting: "Thing_3" is stored as ("Thing", 4). Digits
    with a leading zero stay in the string, matching FName::SplitName."""
    base, sep, digits = text.rpartition("_")
    if (sep and digits.isdigit() and base
            and (digits == "0" or not digits.startswith("0"))
            and len(digits) < 10):
        return base, int(digits) + 1
    return text, 0


def _fstring(text):
    if not text:
        return struct.pack("<i", 0)
    raw = text.encode("utf-8") + b"\0"
    return struct.pack("<i", len(raw)) + raw


def build(assets):
    """
    The registry as bytes. `assets` is one dict per asset, in order:

        object_path    /Mod/Folder/Asset.Asset
        package_path   /Mod/Folder
        class_name     Texture2D, SkeletalMesh, PDA_ModMetaData_C, ...
        package_name   /Mod/Folder/Asset
        asset_name     Asset
        tags           [(key, value), ...] -- plain strings
        flags          optional package flags -- real registries carry
                       0x40000 on Blueprint assets
    """
    names, index = [], {}

    def fname(text):
        base, number = split_name(text)
        if base not in index:
            index[base] = len(names)
            names.append(base)
        return struct.pack("<II", index[base], number)

    body = struct.pack("<i", len(assets))
    for a in assets:
        body += fname(a["object_path"])
        body += fname(a["package_path"])
        body += fname(a["class_name"])
        body += fname(a["package_name"])
        body += fname(a["asset_name"])
        body += struct.pack("<i", len(a["tags"]))
        for key, value in a["tags"]:
            body += fname(key) + _fstring(value)
        body += struct.pack("<iiI", 1, 0, a.get("flags", 0))  # chunk ids [0]
    body += TAIL

    table = struct.pack("<i", len(names))
    for n in names:
        table += (_fstring(n)
                  + struct.pack("<HH", _strihash(n) & 0xFFFF,
                                _strcrc32(n) & 0xFFFF))

    head = VERSION_GUID + struct.pack("<Iq", VERSION, 28 + len(body))
    return head + body + table
