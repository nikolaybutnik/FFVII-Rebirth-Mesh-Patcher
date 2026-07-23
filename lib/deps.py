"""
deps.py -- spots mods that reference packages nothing installed provides.

Costume mods often keep their skin textures in a separate companion mod. Without
it the costume still loads, but the skin renders as grey checkers -- and nothing
tells the user why. The container header lists which packages each package
imports, so the gap is detectable up front.

Working mods routinely carry a few imports that resolve nowhere (cook-time
template references the game ignores), so an unresolved import alone proves
nothing. Only IDs belonging to KNOWN companion mods are reported; the rest are
exposed for --debug.
"""

import glob
import os
import struct
import sys

# config lives in the repo root; needed when this file is run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

# Package IDs shipped by known companion mods. A package ID is a hash of the
# package's path, so it is identical for every user of that mod.
KNOWN_COMPANIONS = {}


def _companion(name, *ids):
    for i in ids:
        KNOWN_COMPANIONS[i] = name


_companion(
    "the Aerith skin texture mod (PC0003_AerithSkinF)",
    0x65BC5674CF986F30, 0xB277BA21A92F7F75, 0x681017EF25119720,
    0x0E22A483E6938B95, 0xAC6393A0F9E1CED4, 0x3CB70CDD8FDDC105,
    0x8E8AC918BA8DBA84, 0x80147E1011B82846, 0x40F714E2B3A955E2,
    0x28D2F75D8FF204B0, 0x3D392873043873F8, 0x2E18527C1939FB5E,
    0xEEF613431F67F0FD, 0x40838FECA8B5827E, 0x58DF52C8576A42A3,
    0xB35905744C02078A, 0x752306CD38DF39B4,
)
_companion(
    "the Tifa skin texture mod (PC0002_TifaSkin)",
    0xFB1369D0FF976BD7, 0x328161923CE7F054, 0x408E615721D48BD1,
    0x080F532570D00232, 0xF5C31FAB3AF5DE72, 0xF9C0FB4245B247F2,
    0xB36044D8F0BA2543, 0x7A2F7DDE7DDC9A42, 0xDBBEC49267220D27,
    0xA0DDF0C9BFA974D3,
)


def _ids(blob):
    """Package IDs from a raw chunk-ID array (12 bytes each, type byte last)."""
    return {struct.unpack_from("<Q", blob, k * 12)[0]
            for k in range(len(blob) // 12) if blob[k * 12 + 11] == 2}


def package_ids(utoc_path):
    """Package chunk IDs in a container, from the .utoc alone (no decompression)."""
    try:
        with open(utoc_path, "rb") as f:
            head = f.read(0x20)
            if head[:16] != b"-==--==--==--==-":
                return set()
            hdr_size, n = struct.unpack_from("<2I", head, 0x14)
            f.seek(hdr_size)
            return _ids(f.read(n * 12))
    except OSError:
        return set()


def installed_ids():
    """Every package ID the game can currently load: the game plus ALL mods,
    including the loader-framework ones the patcher itself skips."""
    ids = set()
    for u in glob.glob(os.path.join(config.GAME_PAKS, "*.utoc")):
        ids |= package_ids(u)
    for u in glob.glob(os.path.join(config.MODS_DIR, "*", "Content", "Paks",
                                    "WindowsNoEditor", "*.utoc")):
        ids |= package_ids(u)
    paks = getattr(config, "MODS_PAKS_DIR", "")
    if paks and os.path.isdir(paks):
        for dirpath, _, files in os.walk(paks):
            for f in files:
                if f.endswith(".utoc"):
                    ids |= package_ids(os.path.join(dirpath, f))
    return ids


def imported_packages(toc):
    """All package IDs this container's header declares as imports."""
    hidx = next((i for i in range(toc.n) if toc.chunk_type(i) == 10), None)
    if hidx is None:
        return set()
    d = toc.read(hidx)
    count = struct.unpack_from("<I", d, 32)[0]
    base = 36 + count * 8 + 4
    out = set()
    for i in range(count):
        o = base + i * 32
        num, rel = struct.unpack_from("<II", d, o + 24)
        if num:
            out |= set(struct.unpack_from(f"<{num}Q", d, o + 24 + rel))
    return out


def missing_requirements(utoc_path, avail):
    """(known companion names missing, other unresolved IDs) for one mod.
    Never raises -- an unreadable container just reports nothing."""
    import iostore
    try:
        toc = iostore.Toc(utoc_path)
        unresolved = imported_packages(toc) - package_ids(utoc_path) - avail
    except Exception:
        return [], set()
    needs = sorted({KNOWN_COMPANIONS[i] for i in unresolved if i in KNOWN_COMPANIONS})
    return needs, {i for i in unresolved if i not in KNOWN_COMPANIONS}


if __name__ == "__main__":
    # Maintainer helper: harvest a companion mod's package IDs as a ready-to-
    # paste _companion() entry.
    #
    #   python lib/deps.py <mod .utoc, folder, or release zip> ["display name"]
    import zipfile

    if len(sys.argv) < 2:
        sys.exit("usage: python deps.py <mod .utoc, folder, or zip> [display name]")
    src = sys.argv[1]
    ids, stem = set(), os.path.splitext(os.path.basename(src.rstrip("\\/")))[0]

    def from_bytes(data):
        if data[:16] != b"-==--==--==--==-":
            return set()
        hdr_size, n = struct.unpack_from("<2I", data, 0x14)
        return _ids(data[hdr_size:hdr_size + n * 12])

    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            for m in z.namelist():
                if m.endswith(".utoc"):
                    ids |= from_bytes(z.read(m))
                    stem = os.path.basename(m)[:-len(".utoc")]
    elif os.path.isdir(src):
        for dirpath, _, files in os.walk(src):
            for f in files:
                if f.endswith(".utoc"):
                    ids |= package_ids(os.path.join(dirpath, f))
                    stem = f[:-len(".utoc")]
    else:
        ids = package_ids(src)
    if not ids:
        sys.exit(f"no packages found in {src} -- is that an IoStore mod?")

    stem = stem.replace("End-WindowsNoEditor", "")
    name = sys.argv[2] if len(sys.argv) > 2 else f"the {stem} mod ({stem})"
    print(f"Paste into KNOWN_COMPANIONS ({len(ids)} packages):\n")
    print(f'_companion(\n    "{name}",')
    row = [f"0x{v:016X}" for v in sorted(ids)]
    for k in range(0, len(row), 3):
        print("    " + ", ".join(row[k:k + 3]) + ",")
    print(")")
