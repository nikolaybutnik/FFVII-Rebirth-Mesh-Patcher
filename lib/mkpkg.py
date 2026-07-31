"""
mkpkg.py -- builds a zen data-asset package from nothing.

Everything else in this toolchain edits packages that a cooker made. The two
registration assets a Dresscode mod needs (DA_ModMetaData and the
PDA_ModData_Character outfit list) have no original to edit when a loose pak
is converted -- they are synthesized here: name table, imports, one export,
the standard bundle block, graph, and a tagged-property payload.

The shape reproduced is exactly what the Dresscode toolchain's own cooker
emits for these assets (proven byte-identical against a real mod's): a sorted
case-insensitive name table, the class import / null padding / template
import layout, and a payload that ends with a NativeClass reference, the
terminator, and a four-byte epilogue.
"""

import struct

import cityhash
import pkgedit
import tagged
from zen import ZenPackage

# Every data asset of this shape carries these regardless of content.
BOILERPLATE_NAMES = [
    "/Script/CoreUObject", "/Script/Engine",
    "BlueprintGeneratedClass", "NativeClass", "None", "Package",
]

# One export, created then serialized: header (first entry 0, count 2),
# then Create(export 0), Serialize(export 0).
BUNDLE_BLOCK = struct.pack("<6I", 0, 2, 0, 0, 0, 1)

# Observed on every cooked data asset: Public | Standalone | Transactional.
OBJECT_FLAGS = 0xB


def build(package_name, export_name, class_pkg, class_obj, props,
          imports, graph, extra_names=(), cooked_hdr_size=None):
    """
    A complete package chunk for one tagged-property data asset.

        package_name  its own /Mod/... path
        export_name   the single export's object name
        class_pkg     package holding the blueprint class (/FF7RML/...)
        class_obj     the class object (PDA_..._C); Default__<class_obj> is
                      the template, per the cooked convention
        props         tagged property spec (see tagged.emit_properties)
        imports       the import table as a list of 64-bit IDs, nulls included
        graph         [(package id, [(from_bundle, to_bundle), ...]), ...]
        extra_names   strings to intern beyond what props needs (an import's
                      path and object name, which imports alone cannot supply)
    """
    wanted = set(BOILERPLATE_NAMES)
    wanted.update([package_name, export_name, class_pkg, class_obj,
                   f"Default__{class_obj}"])
    wanted.update(extra_names)
    tagged.collect_names(props, wanted)
    names = sorted(wanted, key=str.lower)
    index = {n: i for i, n in enumerate(names)}

    payload = (tagged.emit_properties(props, index.__getitem__)
               + struct.pack("<II", index["None"], 0)
               + b"\0\0\0\0")

    strings, hashes = pkgedit.build_name_batch(names)
    nm_off = struct.calcsize(ZenPackage.HEADER)
    nh_off = (nm_off + len(strings) + 7) & ~7
    imp_off = nh_off + len(hashes)
    exp_off = imp_off + len(imports) * 8
    bundles_off = exp_off + 72
    graph_off = bundles_off + len(BUNDLE_BLOCK)

    graph_blob = struct.pack("<I", len(graph))
    for pid, arcs in graph:
        graph_blob += struct.pack("<QI", pid, len(arcs))
        for frm, to in arcs:
            graph_blob += struct.pack("<II", frm & 0xFFFFFFFF, to)

    header_end = graph_off + len(graph_blob)
    if cooked_hdr_size is None:
        cooked_hdr_size = header_end

    own_name = index[package_name]
    export = struct.pack(
        "<QQIIQQQQQI4x",
        cooked_hdr_size, len(payload),               # serial offset and size
        index[export_name], 0,                       # FMappedName
        cityhash.NULL_INDEX,                         # outer
        cityhash.object_id(class_pkg, class_obj),
        cityhash.NULL_INDEX,                         # super
        cityhash.object_id(class_pkg, f"Default__{class_obj}"),
        cityhash.object_id(package_name, export_name),
        OBJECT_FLAGS)

    out = bytearray()
    out += struct.pack(ZenPackage.HEADER,
                       own_name, own_name, 0x80000000, cooked_hdr_size,
                       nm_off, len(strings), nh_off, len(hashes),
                       imp_off, exp_off, bundles_off,
                       graph_off, len(graph_blob), 0)
    out += strings
    out += b"\0" * (nh_off - nm_off - len(strings))
    out += hashes
    out += struct.pack(f"<{len(imports)}Q", *imports)
    out += export
    out += BUNDLE_BLOCK
    out += graph_blob
    out += payload
    return bytes(out)
