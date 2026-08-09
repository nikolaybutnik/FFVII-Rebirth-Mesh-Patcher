"""
matpack.py -- reads the material-swap machinery Dresscode toggles are made of.

A Dresscode outfit row with an "actor" points at a tiny blueprint whose only
job is to apply an EndMaterialPack (a native /Script/EndGame class): a map of
MESH MATERIAL SLOT NAME -> replacement MaterialInstanceConstant. "Hide the
jacket" is a pack whose replacement is a fully transparent material the mod
itself ships; color options are packs pointing at recolored materials.

The pre-Dresscode modular standard did the same thing with load-order paks
that OVERRIDE a material package outright. Reading the pack (here) and the
mesh's slot->material table (also here) is what lets the converter translate
one into the other.
"""

import struct

import cityhash
import tagged
from zen import ZenPackage

MATERIAL_PACK = cityhash.object_id("/Script/EndGame", "EndMaterialPack", 1)


def is_material_pack(data):
    """Does this package's single export instance EndMaterialPack?"""
    try:
        z = ZenPackage(data)
    except Exception:
        return False
    return any(e["cls"] == MATERIAL_PACK for e in z.exports)


def read_material_pack(data):
    """
    {slot name: replacement object import id} from an EndMaterialPack.

    The export carries one tagged property, MaterialAssets, a
    Name->ObjectProperty map; values are import-table references.
    """
    z = ZenPackage(data)
    span = z.find_export_payload(MATERIAL_PACK)
    if span is None:
        return {}
    r = tagged.Reader(data, span[0], z)
    out = {}
    while r.o < span[1] - 8:
        tag = r.name()
        if tag == "None":
            break
        typ = r.name()
        size = r.i32()
        r.i32()
        if typ == "MapProperty":
            r.name(), r.name()
        r.u8()
        end = r.o + size
        if tag == "MaterialAssets" and typ == "MapProperty":
            r.i32()                                 # entries removed
            count = r.i32()
            for _ in range(count):
                slot = r.name()
                idx = r.i32()
                if -len(z.imports) <= idx <= -1:
                    out[slot] = z.imports[-idx - 1]
        r.o = end
    return out


def material_slots(data, limit=1 << 20):
    """
    [(slot name, material object import id, entry byte offset)] -- the
    mesh's material table. The offset is where the entry's FPackageIndex
    sits, so a caller can REPOINT a slot at another material -- the same
    per-slot swap a Dresscode toggle performs, baked into the mesh.

    FF7R's cooked FSkeletalMaterial is 40 bytes: an import index (i32), the
    slot FName, then 28 bytes of UV density data. There is no reliable
    anchor in front of it, so this finds the LONGEST run of consecutive
    valid entries near the start of the export data -- validated across
    several mods' meshes; wrong candidates never chain.
    """
    z = ZenPackage(data)
    n_names, n_imp = len(z.names), len(z.imports)

    def entry(o):
        idx = struct.unpack_from("<i", data, o)[0]
        if not (-n_imp <= idx <= -1):
            return None
        ni, nn = struct.unpack_from("<II", data, o + 4)
        if (ni & 0x3FFFFFFF) >= n_names or nn > 1000:
            return None
        imp = z.imports[-idx - 1]
        if imp >> 62 != 2:
            return None
        return (z.name_at(ni, nn), imp, o)

    start = z.export_data_start()
    end = min(len(data) - 40, start + limit)
    best = []
    o = start
    while o < end:                          # unaligned: step by single bytes
        run = []
        p = o
        while p + 40 <= len(data):
            row = entry(p)
            if not row:
                break
            run.append(row)
            p += 40
        if len(run) > len(best):
            best = run
            o = p
        o += 1
    return best if len(best) >= 2 else []


def repoint_slots(data, slot_to_import, dep_pids=()):
    """
    The mesh with the given slots' materials swapped: {slot name: object
    import id}. Missing ids are appended to the import table, and
    `dep_pids` (the replacements' package IDs) gain graph entries so the
    loader actually sequences those packages in -- import, graph and the
    container-header dependency all three, or the material silently fails
    to resolve. This IS what a Dresscode toggle does at runtime, made
    permanent.
    """
    import pkgedit
    z = ZenPackage(data)
    have = set()
    if z.graph_size >= 4:
        o = z.graph_off
        n = struct.unpack_from("<I", data, o)[0]
        o += 4
        for _ in range(n):
            if o + 12 > z.graph_off + z.graph_size:
                break
            have.add(struct.unpack_from("<Q", data, o)[0])
            narcs = struct.unpack_from("<I", data, o + 8)[0]
            o += 12 + narcs * 8
    missing = [i for i in set(slot_to_import.values())
               if i not in z.imports]
    new_graph = [(pid, [(0, 0)]) for pid in dep_pids if pid not in have]
    if missing or new_graph:
        data = pkgedit.rewrite(data, extra_imports=missing,
                               extra_graph=new_graph)
        z = ZenPackage(data)
    out = bytearray(data)
    for slot, imp, off in material_slots(data):
        want = slot_to_import.get(slot)
        if want is not None:
            struct.pack_into("<i", out, off, -(z.imports.index(want) + 1))
    return bytes(out)


def object_resolver(toc):
    """
    {object import id: (package name, object name)} for everything a
    container's packages export, plus every hashable name-table candidate --
    stock materials included, since a package's object is almost always
    named after its leaf.
    """
    out = {}
    for i in range(toc.n):
        if toc.chunk_ids[i][11] != 2:
            continue
        z = ZenPackage(toc.read(i))
        pkg = z.names[z.name & 0x3FFFFFFF]
        for e in z.exports:
            if e["gimp"] != cityhash.NULL_INDEX:
                out[e["gimp"]] = (pkg, e["name"])
        for n in z.names:
            if n.startswith("/") and not n.startswith("/Script/"):
                leaf = n.rsplit("/", 1)[-1]
                out.setdefault(cityhash.object_id(n, leaf), (n, leaf))
    return out
