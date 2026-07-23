"""
meshfix.py -- converts a pre-1.005 skeletal mesh to the 1.005+ layout.

Three things changed. This header covers the first, which is what makes the game
crash; the other two are documented beside the code that handles them, further
down:

    1. render sections no longer carry FDuplicatedVerticesBuffer   (below)
    2. the tangent buffer is 4 bytes per vertex, in a new encoding (CHANGE #2)
    3. texture coordinates are half floats, never float32          (CHANGE #3)

CHANGE #1 -- THE ONE THAT CRASHES
---------------------------------
This is the thing the mod author referred to as "patch V1.005 changed the
structure of skeletal meshes".

A skeletal mesh is split into RENDER SECTIONS -- runs of triangles sharing one
material. Each section ends with a small tail. In mods built before the patch that
tail contains an extra structure, FDuplicatedVerticesBuffer:

    ClothingData            FGuid (16 bytes) + AssetLodIndex (4 bytes)
    DupVertData             uint32 count + count * 4 bytes     <-- EXTRA
    DupVertIndexData        uint32 count + count * 8 bytes     <-- EXTRA
    bDisabled               uint32

The game's own 1.005 meshes DO NOT have those two arrays. Their tail is just
ClothingData followed by bDisabled.

HOW WE KNOW
-----------
Parsing one of FFVII Rebirth's own character meshes with no duplicated-vertex
arrays makes all 12 of its sections chain perfectly: each section's baseIndex
equals the running total of triangles*3.

(That baseIndex chain is the reliable check. Material indices happen to run
0, 1, 2, ... on the game's own meshes, but that is NOT a rule -- some mods leave
material slots unused and skip numbers. See detect_dup_verts.)

Parsing the mods' meshes WITH those arrays makes their sections chain perfectly
too -- and in every single section the DupVertIndexData count comes out exactly
equal to that section's vertex count. That is not a coincidence you get from a
misparse; it's the structure being read correctly.

So the mods write two arrays the current game doesn't read. The reader desyncs
immediately after the first section's ClothingData, then interprets vertex data as
structure -- which is why hovering a broken costume in Dresscode is a hard crash
rather than a missing model.

THE FIX
-------
For every section: delete the two arrays and set the section's class strip flag
to 1, which is the value the game's own meshes carry.

That combination is correct whichever way the engine actually behaves:

  - If 1.005 deleted the field outright, removing the bytes is what's needed and
    the flag is ignored.
  - If 1.005 instead made the field conditional on that flag, then flag=1 means
    "not present" and removing the bytes is again correct.

Either way we emit byte-for-byte what the retail game emits, which is the safest
possible target.

LIMITATION
----------
Only meshes with a single LOD are handled. Walking to LOD 1 would mean parsing the
whole vertex/index buffer block, which we never fully mapped. Every costume mod
checked so far ships exactly one LOD, so this covers them; the patcher refuses
loudly rather than guessing if it meets a multi-LOD mesh.
"""

import struct

import skm

CLASS_STRIP_DUPLICATED_VERTICES = 1


# ---------------------------------------------------------------------------
# CHANGE #2 -- the tangent buffer.
#
# Stock UE 4.26 stores a tangent basis as two FPackedNormals (TangentX and
# TangentZ), 8 bytes per vertex, or two FPackedRGBA16N (16 bytes) when a mesh
# opts into bUseHighPrecisionTangentBasis.
#
# Every FFVII Rebirth 1.005 mesh uses 4 bytes per vertex instead, in a new
# encoding, and never sets the high-precision flag -- verified across 60 game
# character meshes at various LODs, vertex counts and UV-set counts.
#
# Two distinct failures follow from getting this wrong:
#
#   WRONG SIZE   The engine compares the stored element size against its own
#                sizeof(). On mismatch it leaves the fast bulk-copy path, the
#                stream desyncs and it overruns the buffer -- an access
#                violation writing to a page-aligned address.
#
#   WRONG VALUES The mesh loads, but the GPU decodes nonsense. Shading is wrong
#                without any crash, which is much harder to diagnose.
#
# shader_decode.py holds the encoding itself, transcribed from the game's own
# vertex shader, along with the evidence for it.
#
#
# CHANGE #3 -- texture coordinate precision.
#
# Mods may set bUseFullPrecisionUVs, storing each UV as two float32 rather than
# two half floats. The game's meshes never do, and its vertex shaders declare
# texcoord inputs as two-byte floats, so full-precision UVs are read as half and
# every texture lookup lands in the wrong place. Converted here to match.
# ---------------------------------------------------------------------------

def _fix_degenerate_tangents(N, T):
    """
    Replace unusable tangents with a deterministic perpendicular one.

    Some meshes ship vertices whose TangentX is exactly zero -- 0.74% of them in
    one real mod. The old 8- and 16-byte formats could store that; the 4-byte
    format cannot, because it always decodes to a unit vector. So rather than
    let a divide-by-almost-zero decide the direction, pick the same fallback the
    decoder would produce for angle 0: the first axis of the reference basis.

    Returns T with the bad rows replaced, and a count of how many were fixed.
    """
    import numpy as np

    n = np.linalg.norm(T, axis=1)
    bad = ~np.isfinite(n) | (n < 1e-6)
    if not bad.any():
        return T, 0

    # Frisvad/Duff basis, matching shader_decode.decode()
    Nb = N[bad]
    Nx, Ny, Nz = Nb[:, 0], Nb[:, 1], Nb[:, 2]
    s = np.where(Nz >= 0.0, -1.0, 1.0)
    a = 1.0 / (Nz - s)
    e1 = np.stack([1.0 + s * Nx * Nx * a, s * (Nx * Ny * a), s * Nx], axis=1)
    e1 /= np.maximum(np.linalg.norm(e1, axis=1, keepdims=True), 1e-12)

    T = T.copy()
    T[bad] = e1
    return T, int(bad.sum())


def convert_tangents_16_to_4(buf):
    """
    High-precision tangents (16 bytes/vertex) -> the 4-byte format.

    Some mods set bUseHighPrecisionTangentBasis, which stores the frame as two
    FPackedRGBA16N values: TangentX and TangentZ, each four signed 16-bit
    components (x, y, z, w), where TangentZ's w carries handedness.

    FFVII Rebirth 1.005 does not use that path -- all 60 game character meshes
    checked report the flag as 0 with a 4-byte element -- and the vertex shader
    reads the stream as R10G10B10A2, so a 16-byte buffer is misinterpreted.
    The visible result is wrong shading (a distinctive over-shiny look) rather
    than a crash, because nothing overruns; the data is simply read wrong.
    """
    import numpy as np
    import shader_decode as _sd

    n = len(buf) // 16
    if n == 0:
        return b""

    v = np.frombuffer(bytes(buf), dtype="<i2", count=n * 8).reshape(n, 8)
    v = v.astype(np.float64)
    T = v[:, 0:3] / 32767.0
    N = v[:, 4:7] / 32767.0
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)

    # project into the tangent plane, then rescue anything degenerate
    T = T - N * np.sum(T * N, axis=1, keepdims=True)
    T, _ = _fix_degenerate_tangents(N, T)
    T /= np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-9)

    positive = v[:, 7] >= 0
    words = _sd.encode(N, T, handed_positive=positive)
    return words.astype("<u4").tobytes()


def convert_uvs_full_to_half(buf, n_values):
    """
    Full-precision texture coordinates (float32) -> half floats.

    Some mods set bUseFullPrecisionUVs, storing each UV as two float32 (8 bytes
    per set per vertex). The game's own meshes never do -- and the vertex shader
    declares its texcoord inputs as two-byte floats -- so full-precision UVs are
    read as half and every texture lookup lands in the wrong place. That shows
    up as wrong colours rather than a crash.
    """
    import numpy as np

    f = np.frombuffer(bytes(buf), dtype="<f4", count=n_values * 2)
    return f.astype("<f2").tobytes()


def convert_tangents_8_to_4(buf):
    """buf: the raw 8-bytes-per-vertex tangent array. Returns 4-bytes-per-vertex.

    Each input vertex is two FPackedNormals: TangentX (the tangent) then
    TangentZ (the normal), whose fourth byte also carries the handedness sign.
    """
    import numpy as np
    import shader_decode as _sd

    n = len(buf) // 8
    if n == 0:
        return b""

    b = np.frombuffer(bytes(buf), dtype=np.uint8, count=n * 8).reshape(n, 8)
    signed = np.where(b > 127, b.astype(np.int16) - 256, b.astype(np.int16))
    sb = signed.astype(np.float64)

    T = sb[:, 0:3] / 127.0
    N = sb[:, 4:7] / 127.0
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)

    # Make the tangent perpendicular to the normal before encoding: the format
    # stores a direction within the tangent plane, so any component along the
    # normal would simply be lost. Vertices whose tangent is unusable get a
    # deterministic fallback rather than whatever a near-zero divide produces.
    T = T - N * np.sum(T * N, axis=1, keepdims=True)
    T, _ = _fix_degenerate_tangents(N, T)
    T /= np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-9)

    # The mod's handedness byte is negative for a mirrored frame; bit31 is set
    # for a positive one (verified at 99.92% against the game's own meshes).
    positive = b[:, 7] < 128

    words = _sd.encode(N, T, handed_positive=positive)
    return words.astype("<u4").tobytes()


def u32(d, o):
    """Read a little-endian uint32 at offset `o`."""
    return struct.unpack_from("<I", d, o)[0]


def parse_section_bounds(data, o, has_dup_verts):
    """
    Parse one render section, returning where its pieces are.

    Returns (info, layout) where layout has:
        start        first byte of the section
        dup_start    first byte of the duplicated-vertex arrays (or None)
        dup_end      first byte after them (== bDisabled)
        end          first byte after the whole section
    """
    start = o
    info, o = skm.parse_section(data, o)
    o += 20                                     # ClothingData
    dup_start = o if has_dup_verts else None
    if has_dup_verts:
        n1 = u32(data, o); o += 4 + n1 * 4      # DupVertData
        n2 = u32(data, o); o += 4 + n2 * 8      # DupVertIndexData
        info["dup"] = (n1, n2)
    dup_end = o
    o += 4                                      # bDisabled
    return info, dict(start=start, dup_start=dup_start, dup_end=dup_end, end=o)


def _sections_chain(data, sections_at, n_sections, has_dup):
    """
    Walk the render sections under one assumption. Returns the offset just past
    the last section, or None if the walk falls apart.

    THE CHECK: each section's baseIndex must equal the running total of
    triangles*3. That is a genuine self-check -- a misparse would have to land
    on the exact right running total for every section in a row, which does not
    happen by accident.

    WHAT WE DELIBERATELY DO NOT CHECK: that material indices run 0, 1, 2, ...
    An earlier version required that, and it was wrong. A mesh may leave
    material slots unused, so the indices can have gaps. One real mod declares
    11 slots and its 10 sections use [0,1,2,3,4,5,6,8,9,10], skipping slot 7.
    That is perfectly legal, and the strict rule rejected the whole mod.
    Material index is only sanity-bounded here; baseIndex does the real work.
    """
    o = sections_at
    expected_base = 0
    try:
        for k in range(n_sections):
            info, layout = parse_section_bounds(data, o, has_dup)
            if info["base_index"] != expected_base:
                return None
            if not (0 <= info["material_index"] < 1024):
                return None
            expected_base += info["n_triangles"] * 3
            o = layout["end"]
    except Exception:
        return None
    return o


def _walk_vertex_buffers(data, sections_end):
    """
    Walk from the end of the render sections to the vertex buffer headers.

    `sections_end` is the offset just past the last section. Returns a dict
    describing the tangent and texcoord buffers, or None if the walk lands
    somewhere implausible -- which is how callers tell a correct section parse
    from an incorrect one.

    This is the only description of that layout. Both the caller that just wants
    the tangent element size and the one that wants every offset go through
    here, so there is no second copy to drift out of sync.
    """
    try:
        o = sections_end
        n_active = u32(data, o)
        if n_active > 100000:                   # ActiveBoneIndices
            return None
        o += 4 + n_active * 2

        q = o + 4 + 2                           # size prefix + stream strip flags
        dts = data[q]; q += 1                   # MultiSizeIndexContainer
        if dts not in (2, 4):
            return None
        q += 4                                  # element size
        icount = u32(data, q); q += 4
        q += icount * dts                       # index data

        q += 16 + u32(data, q + 12) * u32(data, q + 8)   # PositionVertexBuffer

        q += 2                                  # StaticMeshVertexBuffer strip flags
        n_tex = u32(data, q); q += 4
        n_verts = u32(data, q); q += 4
        full_uv_off = q
        full_uv = u32(data, q); q += 4          # bUseFullPrecisionUVs
        high_tan_off = q
        high_tan = u32(data, q); q += 4         # bUseHighPrecisionTangentBasis

        elem_off = q
        elem = u32(data, q)
        count = u32(data, q + 4)

        # The texcoord buffer follows the tangent buffer, with its own
        # (element size, count) header.
        uv_elem_off = q + 8 + elem * count
        uv_elem = u32(data, uv_elem_off)
        uv_count = u32(data, uv_elem_off + 4)
    except Exception:
        return None

    return dict(n_tex=n_tex, n_verts=n_verts,
                full_uv=full_uv, full_uv_off=full_uv_off,
                high_tan=high_tan, high_tan_off=high_tan_off,
                elem_off=elem_off, elem=elem, count=count, data=elem_off + 8,
                uv_elem_off=uv_elem_off, uv_elem=uv_elem, uv_count=uv_count,
                uv_data=uv_elem_off + 8)


def detect_dup_verts(data, sections_at, n_sections):
    """
    Work out whether this mesh's sections carry the duplicated-vertex arrays.

    Try both assumptions and keep whichever makes the sections chain up.

    THE SUBTLETY: with only ONE render section the chain check cannot tell the
    two apart. Its only assertions are "material index is 0" and "baseIndex is
    0", and both hold whichever way the tail is read -- there is no second
    section to desync into. A mesh like that would be reported as unpatched
    forever, and re-patching it would corrupt it.

    So when both assumptions chain, we break the tie on the tangent buffer's
    element size, which is unambiguous: 8 in the old format, 4 in the new one.
    """
    candidates = []
    for has_dup in (True, False):
        end = _sections_chain(data, sections_at, n_sections, has_dup)
        if end is not None:
            candidates.append((has_dup, end))

    if not candidates:
        raise ValueError("could not parse render sections either way")

    if len(candidates) == 1:
        return candidates[0]

    # Ambiguous -- decide on the tangent element size.
    for has_dup, end in candidates:
        buffers = _walk_vertex_buffers(data, end)
        expected = 8 if has_dup else 4
        if buffers and buffers["elem"] == expected:
            return has_dup, end

    # Still ambiguous: prefer the reading that yields a legal element size at
    # all, and failing that assume the mesh is already converted, since
    # re-converting an already-converted mesh is the destructive mistake.
    for has_dup, end in candidates:
        buffers = _walk_vertex_buffers(data, end)
        if buffers and buffers["elem"] in (4, 8, 16):
            return has_dup, end
    return False, dict(candidates)[False]


def old_format(payload, sections_at, n_sections):
    """True if this mesh still has ANY pre-1.005 trait: dup-verts arrays (the
    crash), 8/16-byte tangents (wrong shading), or full-precision UVs (wrong
    textures). convert_payload fixes all three, so "needs patching" must test
    all three -- dup arrays alone miss meshes that were partially hand-fixed."""
    has_dup, end = detect_dup_verts(payload, sections_at, n_sections)
    if has_dup:
        return True
    buf = _walk_vertex_buffers(payload, end)
    return buf is not None and bool(buf["elem"] in (8, 16)
                                    or (buf["full_uv"] and buf["uv_elem"] == 8))


def convert_payload(payload):
    """
    Convert one skeletal mesh export payload from the old layout to the new one.

    `payload` is just the object's bytes. Returns (new_payload, report) or
    (payload, report) unchanged if it's already in the new format.
    """
    report = {}

    after_skeleton, info = skm.parse_head(payload, 0, len(payload),
                                          skm.NoNames(), verbose=False)
    lod = skm.parse_lod_header(payload, after_skeleton)

    report["n_lods"] = lod["n_lods"]
    report["n_sections"] = lod["n_sections"]
    report["n_bones"] = info["n_bones"]

    if lod["n_lods"] != 1:
        raise NotImplementedError(
            f"mesh has {lod['n_lods']} LODs; only single-LOD meshes are supported "
            "(see the LIMITATION note in meshfix.py)")

    has_dup, sections_end = detect_dup_verts(
        payload, lod["sections_at"], lod["n_sections"])
    report["had_duplicated_vertices"] = has_dup

    removed = 0

    # --- CHANGE #1: strip the duplicated-vertex arrays from every section. ---
    if has_dup:
        out = bytearray()
        o = lod["sections_at"]
        for _ in range(lod["n_sections"]):
            _, layout = parse_section_bounds(payload, o, True)
            body = bytearray(payload[layout["start"]:layout["dup_start"]])
            # byte 1 of the section is the class strip flag; game meshes use 1.
            body[1] |= CLASS_STRIP_DUPLICATED_VERTICES
            out += body
            out += payload[layout["dup_end"]:layout["end"]]      # bDisabled
            removed += layout["dup_end"] - layout["dup_start"]
            o = layout["end"]
        assert o == sections_end
        payload = payload[:lod["sections_at"]] + bytes(out) + payload[sections_end:]

    # --- CHANGE #3: full-precision UVs (float32) -> half floats. -------------
    #
    # Done BEFORE the tangent conversion because it sits later in the buffer;
    # converting it first leaves the tangent buffer's offsets untouched.
    loc = locate_tangent_buffer(payload)
    report["uv_elem"] = loc["uv_elem"]
    report["full_precision_uvs"] = loc["full_uv"]
    if loc["full_uv"] and loc["uv_elem"] == 8:
        old = payload[loc["uv_data"]:loc["uv_data"] + loc["uv_elem"] * loc["uv_count"]]
        new = convert_uvs_full_to_half(old, loc["uv_count"])
        payload = (payload[:loc["uv_elem_off"]]
                   + struct.pack("<II", 4, loc["uv_count"])
                   + new
                   + payload[loc["uv_data"] + len(old):])
        # clear the flag so the engine reads the buffer the way we wrote it
        payload = (payload[:loc["full_uv_off"]] + struct.pack("<I", 0)
                   + payload[loc["full_uv_off"] + 4:])
        removed += len(old) - len(new)
        report["uvs_converted"] = loc["uv_count"]

    # --- CHANGE #2: shrink the tangent buffer to 4 bytes per vertex. ---------
    loc = locate_tangent_buffer(payload)
    report["tangent_elem"] = loc["elem"]
    report["high_precision_tangents"] = loc["high_tan"]
    if loc["elem"] in (8, 16):
        old = payload[loc["data"]:loc["data"] + loc["elem"] * loc["count"]]
        if loc["elem"] == 16:
            new = convert_tangents_16_to_4(old)
        else:
            new = convert_tangents_8_to_4(old)
        payload = (payload[:loc["elem_off"]]
                   + struct.pack("<II", 4, loc["count"])
                   + new
                   + payload[loc["data"] + len(old):])
        # clear the high-precision flag; the buffer is now the standard format
        if loc["high_tan"]:
            payload = (payload[:loc["high_tan_off"]] + struct.pack("<I", 0)
                       + payload[loc["high_tan_off"] + 4:])
        removed += len(old) - len(new)
        report["tangents_converted"] = loc["count"]

    report["changed"] = removed > 0
    report["bytes_removed"] = removed
    return payload, report


def locate_tangent_buffer(payload):
    """
    Walk LOD0 to the vertex buffer headers and report where everything sits.

    Assumes the duplicated-vertex arrays are already gone (call after CHANGE #1).
    Raises if the layout cannot be read, which callers treat as "leave this mesh
    alone" rather than guessing at it.
    """
    after_skeleton, _ = skm.parse_head(payload, 0, len(payload),
                                       skm.NoNames(), verbose=False)
    lod = skm.parse_lod_header(payload, after_skeleton)

    o = lod["sections_at"]
    for _ in range(lod["n_sections"]):
        _, layout = parse_section_bounds(payload, o, False)
        o = layout["end"]

    buffers = _walk_vertex_buffers(payload, o)
    if buffers is None:
        raise ValueError("could not locate the vertex buffers")
    return buffers
