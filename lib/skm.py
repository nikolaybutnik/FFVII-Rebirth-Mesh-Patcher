"""
skm.py -- parses the structure of a skeletal mesh.

Locates the parts of a mesh that the patcher needs to rewrite. meshfix.py does
the rewriting; this module just says where things are.

WHAT IT FINDS
-------------
A skeletal mesh export is laid out, after its properties:

    FStripDataFlags     2 bytes    what was stripped when cooking
    FBoxSphereBounds    28 bytes   origin(3 floats) extent(3) radius(1)
    Materials           array, 40 bytes each
    FReferenceSkeleton  bones, their poses, and a name lookup
    bCooked             int32
    LOD data            geometry -- one entry per level of detail

parse_head() walks as far as the LOD data and reports the material and bone
counts on the way. parse_lod_header() then finds the render sections, and
parse_section() reads one.

WHY THE BOUNDS ARE FOUND BY SEARCHING
-------------------------------------
The property block before the bounds is variable-length and not fully mapped, so
find_bounds() scans for the FBoxSphereBounds signature instead: a strip-flags
pair followed by seven floats where the radius agrees with the extent. That is a
strong enough shape to be unambiguous in practice, and it means the parser does
not depend on decoding every property Unreal might emit.
"""

import math
import struct

import globals_meta

SKELETAL_MESH = globals_meta.SKELETAL_MESH


class NoNames:
    """
    A stand-in for a package when only the mesh STRUCTURE is wanted.

    parse_head() consults the package solely to turn name indices into readable
    material and bone names. Callers that just need offsets and counts pass this
    instead, which keeps them from having to load a real package.
    """

    def name_at(self, index, number=0):
        return ""



def find_bounds(data, start, end):
    """
    Locate the bounding box, which acts as our anchor into the binary section.

    Why we search instead of calculating: for mod packages we could walk the
    tagged properties to find where they end, but the game's packages use the
    unversioned format which we can't decode without the class schema. Searching
    for a recognisable signature works for both.

    The signature:
      - byte 0x01 then 0x00 -- FStripDataFlags, where 1 means "editor data
        stripped", which is true of any cooked asset
      - then 7 floats: origin (3), box extent (3), sphere radius (1)

    The clincher is the last test: for a real bounding box the radius must equal
    the length of the extent vector. Random bytes essentially never satisfy that,
    which makes this reliable rather than merely suggestive.

    Confirmed on a real cooked asset: extent (1,1,1), radius 1.732 = sqrt(3) --
    a unit cube.
    """
    for o in range(start, min(start + 8000, end - 40)):
        if data[o] != 1 or data[o + 1] != 0:
            continue
        v = struct.unpack_from("<7f", data, o + 2)
        if not all(math.isfinite(x) for x in v):
            continue
        ox, oy, oz, ex, ey, ez, radius = v
        if ex <= 0 or ey <= 0 or ez <= 0 or radius <= 0:
            continue
        if not (0.5 < radius < 100000):
            continue
        if abs(math.sqrt(ex * ex + ey * ey + ez * ez) - radius) / radius < 0.02 \
                and abs(ox) < 10000:
            return o
    return None


def _find_stub_bounds(data, start, end):
    """
    Fallback for stub meshes ("remove X"/"invisible X" mods), whose bounds can
    sit below find_bounds()'s 0.5 radius floor. Runs only when the strict pass
    found nothing, so existing anchors never change. The floor guarded against
    float noise; in its place a hit must be followed by a sane material count,
    where parse_head() will read it.
    """
    for o in range(start, min(start + 8000, end - 44)):
        if data[o] != 1 or data[o + 1] != 0:
            continue
        v = struct.unpack_from("<7f", data, o + 2)
        if not all(math.isfinite(x) for x in v):
            continue
        ox, oy, oz, ex, ey, ez, radius = v
        if ex <= 0 or ey <= 0 or ez <= 0 or not (1e-4 < radius < 100000):
            continue
        if abs(math.sqrt(ex * ex + ey * ey + ez * ez) - radius) / radius < 0.02 \
                and abs(ox) < 10000:
            if 0 <= struct.unpack_from("<i", data, o + 30)[0] <= 64:
                return o
    return None


def parse_head(data, start, end, pkg, verbose=True):
    """
    Parse from the bounds through to the end of the skeleton.

    Returns (offset_after_skeleton, info_dict).
    """
    bounds_at = find_bounds(data, start, end)
    if bounds_at is None:
        bounds_at = _find_stub_bounds(data, start, end)
    if bounds_at is None:
        # An all-zero box is a mesh hollowed out on purpose (an "invisible"
        # mod) -- name that instead of failing generically.
        if data.find(b"\x01\x00" + b"\x00" * 28,
                     start, min(start + 8000, end)) != -1:
            raise ValueError("this mod's model is an empty placeholder (an "
                             "'invisible'-type mod) with nothing left for "
                             "this tool to check -- if the game crashes with "
                             "it installed, remove it or ask its author for "
                             "a V1.005 version")
        raise ValueError("could not find the model data in this mod -- "
                         "please report this mod")

    o = bounds_at + 2 + 28          # skip strip flags + bounds

    # --- Materials: 40 bytes each. A negative object index means the material
    #     lives in another package (an import).
    n_materials = struct.unpack_from("<i", data, o)[0]; o += 4
    materials = []
    for _ in range(n_materials):
        obj_index = struct.unpack_from("<i", data, o)[0]
        name_i, name_n = struct.unpack_from("<II", data, o + 4)
        uv_data = struct.unpack_from("<ii4f", data, o + 12)
        materials.append((obj_index, pkg.name_at(name_i, name_n), uv_data))
        o += 40

    # --- Skeleton: bone list, then their rest poses, then a name->index map.
    n_bones = struct.unpack_from("<i", data, o)[0]; o += 4
    bones = []
    for _ in range(n_bones):
        name_i, name_n, parent = struct.unpack_from("<IIi", data, o)
        o += 12
        bones.append((pkg.name_at(name_i, name_n), parent))

    n_pose = struct.unpack_from("<i", data, o)[0]; o += 4
    o += n_pose * 40            # FTransform: quat 16 + translation 12 + scale 12

    n_namemap = struct.unpack_from("<i", data, o)[0]; o += 4
    o += n_namemap * 12

    if verbose:
        print(f"  bounds@{bounds_at}  materials={n_materials} bones={n_bones} "
              f"pose={n_pose} nameMap={n_namemap}")
        for m in materials[:3]:
            print(f"     material objIndex={m[0]} slot='{m[1]}'")
        for b in bones[:4]:
            print(f"     bone '{b[0]}' parent={b[1]}")
        print(f"  skeleton ends at {o}; {end - o} bytes of geometry follow")

    return o, dict(bounds=bounds_at, n_materials=n_materials, n_bones=n_bones,
                   n_pose=n_pose, n_namemap=n_namemap,
                   materials=materials, bones=bones)


def parse_lod_header(data, o):
    """
    Parse the start of the geometry: LOD count, then LOD 0's header.

    Both mod and game meshes decode consistently here.
    """
    is_cooked = struct.unpack_from("<I", data, o)[0]
    n_lods = struct.unpack_from("<I", data, o + 4)[0]
    q = o + 8
    strip_flags = (data[q], data[q + 1])
    q += 10                 # strip flags + 8 bytes of inline/streamed flags

    n_required = struct.unpack_from("<I", data, q)[0]; q += 4
    required_bones = [struct.unpack_from("<H", data, q + 2 * i)[0]
                      for i in range(min(n_required, 8))]
    q += n_required * 2

    n_sections = struct.unpack_from("<I", data, q)[0]; q += 4

    return dict(is_cooked=is_cooked, n_lods=n_lods, strip_flags=strip_flags,
                n_required_bones=n_required, required_bones_sample=required_bones,
                n_sections=n_sections, sections_at=q)


def parse_section(data, o):
    """
    Parse one render section -- a run of triangles sharing a material.

    The tail (cloth data, duplicated-vertex arrays) is variable-length and not
    fully mapped, so this stops after the fields we could confirm.
    """
    strip = (data[o], data[o + 1]); o += 2
    material_index = struct.unpack_from("<H", data, o)[0]; o += 2
    base_index = struct.unpack_from("<I", data, o)[0]; o += 4
    n_triangles = struct.unpack_from("<I", data, o)[0]; o += 4
    recompute_tangent = struct.unpack_from("<I", data, o)[0]; o += 4
    mask_channel = data[o]; o += 1
    cast_shadow = struct.unpack_from("<I", data, o)[0]; o += 4
    base_vertex = struct.unpack_from("<I", data, o)[0]; o += 4
    n_cloth = struct.unpack_from("<I", data, o)[0]; o += 4
    o += 4                                                  # unidentified field
    n_bonemap = struct.unpack_from("<I", data, o)[0]; o += 4
    bone_map = [struct.unpack_from("<H", data, o + 2 * i)[0] for i in range(n_bonemap)]
    o += n_bonemap * 2
    n_vertices = struct.unpack_from("<I", data, o)[0]; o += 4
    max_influences = struct.unpack_from("<I", data, o)[0]; o += 4
    cloth_asset = struct.unpack_from("<h", data, o)[0]; o += 2

    return dict(strip=strip, material_index=material_index, base_index=base_index,
                n_triangles=n_triangles, cast_shadow=cast_shadow,
                base_vertex=base_vertex, bone_map=bone_map,
                n_vertices=n_vertices, max_influences=max_influences,
                cloth_asset=cloth_asset), o
