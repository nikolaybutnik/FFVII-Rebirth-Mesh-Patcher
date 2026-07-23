"""
globals_meta.py -- resolves numeric class IDs into readable names.

THE PROBLEM THIS SOLVES
-----------------------
When you parse a package, each export tells you its class as a raw 64-bit number:

    export "Empty"  cls=0x42d93e6acfce1861

Useless on its own. To learn that 0x42d93e6acfce1861 means "SkeletalMesh" you need
a lookup table -- and that table doesn't live in the mod. It lives in the GAME, in
a file called global.utoc.

So: to understand a mod you must read the game it was built for. This module loads
that table once and caches it.

"""

import struct

import config
import iostore
import zen

# Chunk types inside global.utoc:
#   7 = script objects (the ID -> name table)
#   8 = the global name strings
#   9 = the global name hashes
CHUNK_SCRIPT_OBJECTS = 7
CHUNK_GLOBAL_NAMES = 8
CHUNK_GLOBAL_NAME_HASHES = 9

_cache = None


def load():
    """
    Load the game's global script object table.

    Returns (names, objects, toc) where `objects` maps
    global_id -> {"name": str, "outer": parent_id}.

    Cached, so calling this repeatedly is cheap.
    """
    global _cache
    if _cache:
        return _cache

    # NOTE: global.utoc has NO directory index (it isn't an "indexed" container),
    # so a parser that assumes one will crash here. iostore.Toc guards for it.
    toc = iostore.Toc(config.GLOBAL_UTOC)

    by_type = {}
    for i, cid in enumerate(toc.chunk_ids):
        by_type.setdefault(cid[11], []).append(i)

    names = zen.load_name_batch(
        toc.read(by_type[CHUNK_GLOBAL_NAMES][0]),
        toc.read(by_type[CHUNK_GLOBAL_NAME_HASHES][0]),
    )

    meta = toc.read(by_type[CHUNK_SCRIPT_OBJECTS][0])
    count = struct.unpack_from("<I", meta, 0)[0]

    objects = {}
    o = 4
    for _ in range(count):
        # CAREFUL: the object NAME comes first, before the global index.
        # Swapping them produces name indices in the billions -- the reliable
        # sign of a field-order mistake.
        (name_i, name_n, global_id, outer,
         cdo_class) = struct.unpack_from("<IIQQQ", meta, o)
        o += 32
        name_i &= 0x3FFFFFFF        # strip the 2-bit type tag
        objects[global_id] = dict(
            name=names[name_i] if name_i < len(names) else f"<{name_i}>",
            outer=outer,
        )

    _cache = (names, objects, toc)
    return _cache


def full_name(objects, global_id):
    """
    Build a full path for an object by walking its chain of parents.

    e.g. 0x42d93e6acfce1861 -> "/Script/Engine/SkeletalMesh"

    The `seen` set guards against a malformed table looping forever.
    """
    parts = []
    seen = set()
    while global_id in objects and global_id not in seen:
        seen.add(global_id)
        parts.append(objects[global_id]["name"])
        global_id = objects[global_id]["outer"]
    return "/".join(reversed(parts)) if parts else f"{global_id:#018x}"


def find_class(objects, class_name):
    """Look up a class ID by name, e.g. find_class(objs, 'SkeletalMesh')."""
    for gid, info in objects.items():
        if info["name"] == class_name:
            return gid
    return None


# The one we care about most. Looked up once at import time so callers can just
# say `globals_meta.SKELETAL_MESH`.
try:
    _names, _objs, _toc = load()
    SKELETAL_MESH = find_class(_objs, "SkeletalMesh")
except Exception:
    # The game is not readable yet -- paths may still be unset, and config.check()
    # will report that properly. Fall back to the known constant so that merely
    # importing this module cannot fail.
    SKELETAL_MESH = 0x42D93E6ACFCE1861


if __name__ == "__main__":
    names, objs, _ = load()
    print(f"global names: {len(names)}   script objects: {len(objs)}")
    for cls in ("SkeletalMesh", "Skeleton", "PhysicsAsset", "Texture2D"):
        gid = find_class(objs, cls)
        print(f"  {cls:14} {gid:#018x}  {full_name(objs, gid)}")
