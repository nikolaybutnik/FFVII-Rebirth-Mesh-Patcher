"""
mkbp.py -- builds the two assets a Dresscode toggle is made of.

A toggle ("hide the jacket", "wings in blue") is an outfit row with an ACTOR.
The actor is a tiny blueprint whose default object carries a MaterialPack: an
EndMaterialPack asset mapping mesh material SLOT names to replacement
materials. Dresscode spawns the actor, reads the pack, and swaps those slots
on the worn outfit.

Converting a modular pak mod the other way has no such assets to copy -- the
old standard hid a part by overriding its material package outright -- so both
are synthesized here, from nothing, exactly as the Dresscode toolchain's own
cooker emits them (proven byte-identical against a real mod's).

The blueprint is fixed scaffolding: a character class, its default object, the
three inherited components, a scene-root variable and the construction script
that mounts it. Only two references vary -- the pack and the outfit mesh.
"""

import struct

import cityhash
import pkgedit
import tagged
from mkpkg import BUNDLE_BLOCK, OBJECT_FLAGS
from zen import ZenPackage

SCRIPT = cityhash.TYPE_SCRIPT_IMPORT
PACKAGE = cityhash.TYPE_PACKAGE_IMPORT

ENDGAME = "/Script/EndGame"
ENGINE = "/Script/Engine"
COREUOBJECT = "/Script/CoreUObject"

# Every pack and blueprint interns these regardless of content: the script
# packages, and the class names of the imports the cooker records.
PACK_NAMES = [COREUOBJECT, ENDGAME, ENGINE, "Class", "Default__EndMaterialPack",
              "EndMaterialPack", "MapProperty", "MaterialAssets",
              "MaterialInstanceConstant", "NameProperty", "None",
              "ObjectProperty", "Package"]

BP_NAMES = [
    COREUOBJECT, ENDGAME, ENGINE,
    "ArrowComponent", "AttachParent", "BlueprintGeneratedClass",
    "CapsuleComponent", "CharacterMesh0", "CharacterMovement", "CharMoveComp",
    "Class", "CollisionCylinder", "ComponentClass", "ComponentTemplate",
    "Default__BlueprintGeneratedClass", "Default__EndPlayerCharacter",
    "Default__SceneComponent", "Default__SCS_Node",
    "Default__SimpleConstructionScript", "DefaultSceneRoot",
    "DefaultSceneRoot_GEN_VARIABLE", "DefaultSceneRootNode",
    "EndCharacterMovementComponent", "EndCharacterRootComponent",
    "EndMaterialPack", "EndPlayerCharacter", "EndSkeletalMeshComponent",
    "Game", "Guid", "InternalVariableName", "MaterialPack", "Mesh",
    "NameProperty", "None", "Object", "ObjectProperty", "Package",
    "RootComponent", "SceneComponent", "SCS_Node", "SimpleConstructionScript",
    "SkeletalMesh", "StructProperty", "VariableGuid",
]


def _script_package(path):
    """A script import of a bare package path -- no object."""
    return (SCRIPT << 62) | (cityhash.cityhash64(path.lower()
                                                 .encode("utf-16-le"))
                             & cityhash.M62)


def _header(names, imports, exports, graph, payload, package_name,
            cooked_hdr_size):
    """The common package skeleton: summary, name batch, tables, data."""
    index = {n: i for i, n in enumerate(names)}
    strings, hashes = pkgedit.build_name_batch(names)
    nm_off = struct.calcsize(ZenPackage.HEADER)
    nh_off = (nm_off + len(strings) + 7) & ~7
    imp_off = nh_off + len(hashes)
    exp_off = imp_off + len(imports) * 8
    bundles_off = exp_off + len(exports) * 72
    bundle_blob = _bundles(len(exports)) if len(exports) > 1 else BUNDLE_BLOCK
    graph_off = bundles_off + len(bundle_blob)

    graph_blob = struct.pack("<I", len(graph))
    for pid, arcs in graph:
        graph_blob += struct.pack("<QI", pid, len(arcs))
        for frm, to in arcs:
            graph_blob += struct.pack("<II", frm, to)

    if cooked_hdr_size is None:
        cooked_hdr_size = graph_off + len(graph_blob)

    out = bytearray()
    out += struct.pack(ZenPackage.HEADER,
                       index[package_name], index[package_name], 0x80000000,
                       cooked_hdr_size, nm_off, len(strings), nh_off,
                       len(hashes), imp_off, exp_off, bundles_off,
                       graph_off, len(graph_blob), 0)
    out += strings
    out += b"\0" * (nh_off - nm_off - len(strings))
    out += hashes
    out += struct.pack(f"<{len(imports)}Q", *imports)
    for e in exports:
        out += e
    out += bundle_blob
    out += graph_blob
    out += payload
    return bytes(out), index, cooked_hdr_size


def _arc(package, external):
    """A dependency arc: -1 when the package is not in this container."""
    return (0xFFFFFFFF if package in external else 0, 0)


def _export(off, size, name_i, cls, template, gimp, flags,
            outer=cityhash.NULL_INDEX, super_=cityhash.NULL_INDEX, name_n=0):
    """One 72-byte export entry. A name is (index, number): number N means
    Unreal appends _(N-1), which is how SCS_Node_0 is stored."""
    return struct.pack("<QQIIQQQQQI4x", off, size, name_i, name_n, outer, cls,
                       super_, template, gimp, flags)


# The loader's schedule for the blueprint's eight objects: (export, command),
# command 0 create and 1 serialize, interleaved exactly as the cooker emits
# them -- the scene root and construction script are built and serialized
# before the class's own default object exists. Export DATA is laid out in
# serialize order, while the export TABLE stays in export order.
BUNDLE_ENTRIES = [(0, 0), (5, 0), (7, 0), (6, 0), (6, 1), (7, 1), (0, 1),
                  (1, 0), (2, 0), (3, 0), (4, 0), (1, 1), (2, 1), (3, 1),
                  (4, 1), (5, 1)]
SERIALIZE_ORDER = [i for i, cmd in BUNDLE_ENTRIES if cmd == 1]


def _bundles(count):
    """The export-bundle block for a toggle blueprint."""
    assert count == len(SERIALIZE_ORDER)
    out = struct.pack("<II", 0, len(BUNDLE_ENTRIES))
    for idx, cmd in BUNDLE_ENTRIES:
        out += struct.pack("<II", idx, cmd)
    return out


def material_pack(package_name, export_name, entries, external=(),
                  cooked_hdr_size=None):
    """
    An EndMaterialPack: `entries` is [(slot name, material package, material
    object)], the slot names being the mesh's own material slots.

    The import table is [class, template, one per material, one per package
    they live in] -- the cooked shape, materials in name order.

    `external` names the packages this container does NOT ship (base-game
    materials): their dependency arc is -1, the way a cooker records a
    reference to something already in the game.
    """
    materials = []
    for _slot, pkg, obj in entries:
        if (pkg, obj) not in materials:
            materials.append((pkg, obj))
    materials.sort(key=lambda m: m[0].lower())
    slot_import = {}
    for slot, pkg, obj in entries:
        slot_import[slot] = -(materials.index((pkg, obj)) + 2 + 1)

    props = [("MaterialAssets", ("map", "NameProperty", "ObjectProperty",
                                 [(slot, slot_import[slot])
                                  for slot, _p, _o in entries]))]
    wanted = set(PACK_NAMES) | {package_name, export_name}
    for pkg, obj in materials:
        wanted.update([pkg, obj])
    tagged.collect_names(props, wanted)
    names = sorted(wanted, key=str.lower)
    index = {n: i for i, n in enumerate(names)}

    payload = (tagged.emit_properties(props, index.__getitem__)
               + struct.pack("<II", index["None"], 0) + b"\0\0\0\0")

    # After the materials themselves comes one slot per package they live in,
    # in name order with the script package among them -- null for every
    # package the cooker does not name outright.
    packages = sorted({p for p, _o in materials} | {ENDGAME}, key=str.lower)
    imports = ([cityhash.object_id(ENDGAME, "EndMaterialPack", SCRIPT),
                cityhash.object_id(ENDGAME, "Default__EndMaterialPack",
                                   SCRIPT)]
               + [cityhash.object_id(p, o, PACKAGE) for p, o in materials]
               + [_script_package(ENDGAME) if p == ENDGAME
                  else cityhash.NULL_INDEX for p in packages])
    # The graph is ordered by package id -- that is how the loader looks a
    # dependency up.
    graph = sorted((cityhash.package_id(p), [_arc(p, external)])
                   for p in {m[0] for m in materials})

    # Two passes: the export entry needs the header size, which needs the
    # export entry's length -- fixed at 72 bytes, so one placeholder does.
    placeholder = _export(0, len(payload), index[export_name], 0, 0, 0, 0)
    _blob, _index, hdr_size = _header(names, imports, [placeholder], graph,
                                      payload, package_name, cooked_hdr_size)
    export = _export(
        hdr_size, len(payload), index[export_name],
        cityhash.object_id(ENDGAME, "EndMaterialPack", SCRIPT),
        cityhash.object_id(ENDGAME, "Default__EndMaterialPack", SCRIPT),
        cityhash.object_id(package_name, export_name, PACKAGE),
        OBJECT_FLAGS)
    blob, _index, _size = _header(names, imports, [export], graph, payload,
                                  package_name, cooked_hdr_size)
    return blob


def toggle_actor(package_name, class_name, pack_package, pack_object,
                 mesh_package, mesh_object, variable_guid=b"\0" * 16,
                 external=(), cooked_hdr_size=None):
    """
    The blueprint a toggle row points at: an EndPlayerCharacter subclass whose
    default object holds `MaterialPack`, with the outfit mesh on its skeletal
    mesh component. `class_name` is the blueprint's class object -- the cooked
    convention is the asset name plus "_C".
    """
    cdo = f"Default__{class_name}"
    wanted = set(BP_NAMES) | {package_name, class_name, cdo, pack_package,
                              pack_object, mesh_package, mesh_object}
    names = sorted(wanted, key=str.lower)
    index = {n: i for i, n in enumerate(names)}

    imports = [
        cityhash.object_id(ENGINE, "Default__BlueprintGeneratedClass", SCRIPT),
        cityhash.object_id(COREUOBJECT, "Object", SCRIPT),
        cityhash.object_id(ENDGAME, "EndCharacterMovementComponent", SCRIPT),
        cityhash.object_id(ENDGAME, "EndCharacterRootComponent", SCRIPT),
        cityhash.object_id(ENDGAME, "EndPlayerCharacter", SCRIPT),
        cityhash.object_id(ENDGAME, "EndSkeletalMeshComponent", SCRIPT),
        cityhash.object_id(ENGINE, "ArrowComponent", SCRIPT),
        cityhash.object_id(ENGINE, "BlueprintGeneratedClass", SCRIPT),
        cityhash.object_id(ENGINE, "SceneComponent", SCRIPT),
        cityhash.object_id(ENGINE, "SCS_Node", SCRIPT),
        cityhash.object_id(ENGINE, "SimpleConstructionScript", SCRIPT),
        cityhash.object_id(ENDGAME, "Default__EndPlayerCharacter/CharMoveComp",
                           SCRIPT),
        cityhash.object_id(ENDGAME,
                           "Default__EndPlayerCharacter/CollisionCylinder",
                           SCRIPT),
        cityhash.object_id(pack_package, pack_object, PACKAGE),
        cityhash.object_id(ENDGAME, "Default__EndPlayerCharacter", SCRIPT),
        cityhash.object_id(ENDGAME,
                           "Default__EndPlayerCharacter/CharacterMesh0",
                           SCRIPT),
        cityhash.NULL_INDEX,
        cityhash.NULL_INDEX,
        _script_package(COREUOBJECT),
        _script_package(ENDGAME),
        _script_package(ENGINE),
        cityhash.object_id(ENGINE, "Default__SceneComponent", SCRIPT),
        cityhash.object_id(ENGINE, "Default__SCS_Node", SCRIPT),
        cityhash.object_id(ENGINE, "Default__SimpleConstructionScript", SCRIPT),
        cityhash.object_id(mesh_package, mesh_object, PACKAGE),
    ]
    PACK_IMPORT, MESH_IMPORT = -14, -25
    SCENE_COMPONENT = -9

    def emit(props, epilogue=True):
        """A property list, its terminator, and -- for everything but the
        objects that carry a native trailer -- the four-byte epilogue."""
        return (tagged.emit_properties(props, index.__getitem__)
                + struct.pack("<II", index["None"], 0)
                + (b"\0\0\0\0" if epilogue else b""))

    # An FPackageIndex is 1-based: +n is export n-1, -n is import n-1.
    scs_node = emit([
        ("ComponentClass", ("obj", SCENE_COMPONENT)),
        ("ComponentTemplate", ("obj", 6)),          # DefaultSceneRoot_GEN_...
        ("VariableGuid", ("struct_raw", "Guid", variable_guid)),
        ("InternalVariableName", ("name", "DefaultSceneRoot")),
    ])
    scs = emit([("DefaultSceneRootNode", ("obj", 7))])       # SCS_Node_0
    # UClass writes its own data after the property list: the class it lives
    # within (EndPlayerCharacter), its flags, the config name, and the
    # trailing pair the engine emits for a cooked blueprint class.
    klass = emit([("SimpleConstructionScript", ("obj", 8))],
                 epilogue=False) + struct.pack(
        "<18I", 0, 0xFFFFFFFB, 0, 0, 0, 0, 0, 0x00840814, 0xFFFFFFFE, 0x20,
        0, 0, 0, 0, index["None"], 0, 1, 2)
    # The default object ends at its terminator -- no epilogue.
    default = emit([
        ("MaterialPack", ("obj", PACK_IMPORT)),
        ("Mesh", ("obj", 5)),                       # CharacterMesh0
        ("CharacterMovement", ("obj", 3)),          # CharMoveComp
        ("CapsuleComponent", ("obj", 4)),           # CollisionCylinder
        ("RootComponent", ("obj", 4)),
    ], epilogue=False)
    empty = emit([])
    char_mesh = emit([
        ("SkeletalMesh", ("obj", MESH_IMPORT)),
        ("AttachParent", ("obj", 4)),               # CollisionCylinder
    ])

    bodies = {0: klass, 1: default, 2: empty, 3: empty, 4: char_mesh,
              5: empty, 6: scs_node, 7: scs}
    payload = b"".join(bodies[i] for i in SERIALIZE_ORDER)

    def gimp(path):
        return cityhash.object_id(package_name, path, PACKAGE)

    # (name, number, class, template, global import, flags, outer, super).
    # `outer` is a plain export index -- null when the object sits at package
    # level; `number` is Unreal's _N name suffix.
    NONE = cityhash.NULL_INDEX
    spec = [
        (class_name, 0, cityhash.object_id(ENGINE, "BlueprintGeneratedClass",
                                        SCRIPT),
         cityhash.object_id(ENGINE, "Default__BlueprintGeneratedClass",
                            SCRIPT),
         gimp(class_name), 0x9, NONE,
         cityhash.object_id(ENDGAME, "EndPlayerCharacter", SCRIPT)),
        (cdo, 0, 0, cityhash.object_id(ENDGAME, "Default__EndPlayerCharacter",
                                       SCRIPT),
         gimp(cdo), 0x31, NONE, NONE),
        ("CharMoveComp", 0,
         cityhash.object_id(ENDGAME, "EndCharacterMovementComponent", SCRIPT),
         cityhash.object_id(ENDGAME, "Default__EndPlayerCharacter/CharMoveComp",
                            SCRIPT),
         gimp(f"{cdo}/CharMoveComp"), 0x40029, 1, NONE),
        ("CollisionCylinder", 0,
         cityhash.object_id(ENDGAME, "EndCharacterRootComponent", SCRIPT),
         cityhash.object_id(
             ENDGAME, "Default__EndPlayerCharacter/CollisionCylinder", SCRIPT),
         gimp(f"{cdo}/CollisionCylinder"), 0x40029, 1, NONE),
        ("CharacterMesh0", 0,
         cityhash.object_id(ENDGAME, "EndSkeletalMeshComponent", SCRIPT),
         cityhash.object_id(
             ENDGAME, "Default__EndPlayerCharacter/CharacterMesh0", SCRIPT),
         gimp(f"{cdo}/CharacterMesh0"), 0x40029, 1, NONE),
        ("DefaultSceneRoot_GEN_VARIABLE", 0,
         cityhash.object_id(ENGINE, "SceneComponent", SCRIPT),
         cityhash.object_id(ENGINE, "Default__SceneComponent", SCRIPT),
         gimp(f"{class_name}/DefaultSceneRoot_GEN_VARIABLE"), 0x29, 0, NONE),
        ("SCS_Node", 1, cityhash.object_id(ENGINE, "SCS_Node", SCRIPT),
         cityhash.object_id(ENGINE, "Default__SCS_Node", SCRIPT),
         NONE, 0x8, 7, NONE),
        ("SimpleConstructionScript", 1,
         cityhash.object_id(ENGINE, "SimpleConstructionScript", SCRIPT),
         cityhash.object_id(ENGINE, "Default__SimpleConstructionScript",
                            SCRIPT),
         NONE, 0x8, 0, NONE),
    ]
    graph = sorted([(cityhash.package_id(pack_package),
                     [_arc(pack_package, external)]),
                    (cityhash.package_id(mesh_package),
                     [_arc(mesh_package, external)])])

    # Offsets run in EXPORT order from the cooked header size, while the data
    # itself is laid out in serialize order.
    def build(hdr_size):
        # The sizing pass has no header size yet; entries are a fixed width,
        # so any offset gives the same layout and the real one follows.
        exports, off = [], hdr_size or 0
        for i, (nm, num, cls, tmpl, g, flags, outer, sup) in enumerate(spec):
            exports.append(_export(off, len(bodies[i]), index[nm], cls, tmpl,
                                   g, flags, outer, sup, num))
            off += len(bodies[i])
        return _header(names, imports, exports, graph, payload, package_name,
                       hdr_size)

    _blob, _index, hdr_size = build(cooked_hdr_size)
    blob, _index, _size = build(hdr_size)
    return blob
