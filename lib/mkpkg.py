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

# The /Script/Engine package's own import ID -- a script import of the bare
# package path, no object.
SCRIPT_ENGINE_PACKAGE = ((1 << 62)
                         | (cityhash.cityhash64(
                             "/script/engine".encode("utf-16-le"))
                            & ((1 << 62) - 1)))


def build_texture(package_name, export_name, width, height, mip,
                  pixel_format="PF_B8G8R8A8", lighting_guid=b"\0" * 16,
                  cooked_hdr_size=None):
    """
    A complete Texture2D package with one inline mip -- the shape every
    Dresscode preview/thumbnail uses, proven byte-identical against two mods'
    textures (PF_B8G8R8A8 and PF_DXT1 donors; the formulas below hold for
    both). `mip` is the raw pixel payload: BGRA rows for PF_B8G8R8A8.

    The mip is inline (bulk flags ForceInlinePayload | SingleUse), so nothing
    references a .ubulk and the package is self-contained. Bulk and skip
    offsets are recorded in ORIGINAL-file space, based at cooked_hdr_size.
    """
    names = sorted({"/Script/CoreUObject", "/Script/Engine", package_name,
                    "BoolProperty", "Class", "Default__Texture2D", "Guid",
                    "ImportedSize", "IntPoint", "LightingGuid", "NeverStream",
                    "None", "Package", pixel_format, export_name,
                    "StructProperty", "Texture2D"}, key=str.lower)
    index = {n: i for i, n in enumerate(names)}

    props = [
        ("ImportedSize", ("struct_raw", "IntPoint",
                          struct.pack("<ii", width, height))),
        ("LightingGuid", ("struct_raw", "Guid", lighting_guid)),
        ("NeverStream", ("bool", True)),
    ]
    tagged_blob = (tagged.emit_properties(props, index.__getitem__)
                   + struct.pack("<II", index["None"], 0) + b"\0\0\0\0")

    strings, hashes = pkgedit.build_name_batch(names)
    nm_off = struct.calcsize(ZenPackage.HEADER)
    nh_off = (nm_off + len(strings) + 7) & ~7
    imp_off = nh_off + len(hashes)
    exp_off = imp_off + 3 * 8
    bundles_off = exp_off + 72
    graph_off = bundles_off + len(BUNDLE_BLOCK)
    header_end = graph_off + 4                       # graph: zero entries
    if cooked_hdr_size is None:
        cooked_hdr_size = header_end

    # FTexturePlatformData ahead of the mip. The lone u64 13 is constant on
    # every cooked texture seen; the skip offset points just past the mip's
    # trailing dimensions, 16 bytes after the pixel data ends.
    head_len = (len(tagged_blob) + 4 + 4 + 8 + 8 + 12
                + 4 + len(pixel_format) + 1 + 8 + 4 + 20)
    native = (b"\x01\x00\x01\x00" + struct.pack("<i", 1)
              + struct.pack("<Q", 13)
              + struct.pack("<Q", cooked_hdr_size + head_len + len(mip) + 16)
              + struct.pack("<iii", width, height, 1)
              + struct.pack("<i", len(pixel_format) + 1)
              + pixel_format.encode("ascii") + b"\0"
              + struct.pack("<ii", 0, 1)             # first mip, mip count
              + struct.pack("<i", 1)                 # mip is cooked
              + struct.pack("<Iii", 0x48, len(mip), len(mip))
              + struct.pack("<Q", cooked_hdr_size + head_len))
    payload = (tagged_blob + native + mip
               + struct.pack("<iiiI", width, height, 1, 0)
               + struct.pack("<II", index["None"], 0))
    assert len(tagged_blob) + len(native) == head_len

    export = struct.pack(
        "<QQIIQQQQQI4x",
        cooked_hdr_size, len(payload),
        index[export_name], 0,
        cityhash.NULL_INDEX,
        cityhash.object_id("/Script/Engine", "Texture2D", 1),
        cityhash.NULL_INDEX,
        cityhash.object_id("/Script/Engine", "Default__Texture2D", 1),
        cityhash.object_id(package_name, export_name),
        OBJECT_FLAGS)

    own_name = index[package_name]
    out = bytearray()
    out += struct.pack(ZenPackage.HEADER,
                       own_name, own_name, 0x80000000, cooked_hdr_size,
                       nm_off, len(strings), nh_off, len(hashes),
                       imp_off, exp_off, bundles_off, graph_off, 4, 0)
    out += strings
    out += b"\0" * (nh_off - nm_off - len(strings))
    out += hashes
    out += struct.pack("<3Q",
                       cityhash.object_id("/Script/Engine", "Texture2D", 1),
                       SCRIPT_ENGINE_PACKAGE,
                       cityhash.object_id("/Script/Engine",
                                          "Default__Texture2D", 1))
    out += export
    out += BUNDLE_BLOCK
    out += struct.pack("<I", 0)
    out += payload
    return bytes(out)


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
