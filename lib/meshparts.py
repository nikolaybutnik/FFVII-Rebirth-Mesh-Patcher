"""
meshparts.py -- reads a mod's model as a list of parts, and switches parts off.

A skeletal mesh is stored as RENDER SECTIONS: runs of triangles that share one
material slot. Those slots are what a costume is made of -- the author names
them, usually following the game's own naming convention, so the section
list reads as a list of the pieces the costume is made of.

Each section ends with a `bDisabled` flag, which is the engine's own per-section
"do not draw this". Setting it is a FOUR BYTE edit that changes no offset, no
length and no checksum input beyond the chunk itself -- the geometry stays in
the file, so switching a part back on is the same edit in reverse. That is why
this is the lever used here rather than deleting triangles: nothing is lost, and
a wrong guess costs a rebuild rather than the model.

WHAT THIS DOES NOT DO
---------------------
Only LOD 0 is described, because that is the only level the format work here
covers (see meshfix). A multi-LOD mesh is reported and refused rather than
half-edited: switching a part off at LOD 0 alone would make it reappear as the
camera pulls back, which looks like the tool being broken.
"""

import struct

import matpack
import meshfix
import pkgedit
import skm
import zen


class Unreadable(Exception):
    """This package holds a mesh we cannot describe -- reported, never guessed."""


def _slot_table(data, toc):
    """{slot index: (slot name, material package)} for one mesh package."""
    resolve = matpack.object_resolver(toc)
    out = {}
    for i, (slot, imp, _off) in enumerate(matpack.material_slots(data)):
        target = resolve.get(imp)
        out[i] = (slot, target[0] if target else "")
    return out


def read_export(data, payload_start, payload, slots):
    """The parts of one skeletal mesh export. Raises Unreadable if it will not
    describe itself."""
    try:
        after, info = skm.parse_head(payload, 0, len(payload), skm.NoNames(),
                                     verbose=False)
        lod = skm.parse_lod_header(payload, after)
        has_dup, _end = meshfix.detect_dup_verts(payload, lod["sections_at"],
                                                 lod["n_sections"])
    except Exception as ex:
        raise Unreadable(str(ex))

    parts = []
    o = lod["sections_at"]
    for _s in range(lod["n_sections"]):
        try:
            sec, layout = meshfix.parse_section_bounds(payload, o, has_dup)
        except Exception as ex:
            raise Unreadable(f"section {_s} did not parse: {ex}")
        slot, material = slots.get(sec["material_index"], ("", ""))
        flag_at = payload_start + layout["dup_end"]
        parts.append(dict(
            slot_index=sec["material_index"],
            slot=slot or f"slot {sec['material_index']}",
            material=material,
            triangles=sec["n_triangles"],
            vertices=sec["n_vertices"],
            disabled=bool(struct.unpack_from("<I", data, flag_at)[0]),
            flag_at=flag_at,
        ))
        o = layout["end"]
    return dict(n_lods=lod["n_lods"], old_format=has_dup, parts=parts)


def read_container(toc):
    """
    Every skeletal mesh in a container, described as parts.

    Returns [dict(chunk, path, export, n_lods, old_format, parts)] plus, for
    anything that would not parse, a record carrying `error` instead of parts.
    Never raises for one bad package -- a mod with three good meshes and one
    odd one is still worth working on.
    """
    out = []
    for i in range(toc.n):
        if toc.chunk_ids[i][11] != 2:          # package chunks only
            continue
        try:
            data = toc.read(i)
            pkg = zen.ZenPackage(data)
        except Exception:
            continue
        if not any(e["cls"] == skm.SKELETAL_MESH for e in pkg.exports):
            continue
        path = toc.paths.get(i) or f"chunk {i}"
        try:
            package = pkgedit.package_name_of(pkg)
        except Exception:
            package = ""
        try:
            slots = _slot_table(data, toc)
        except Exception:
            slots = {}
        offset = pkg.export_data_start()
        for e in pkg.exports:
            if e["cls"] != skm.SKELETAL_MESH:
                offset += e["size"]
                continue
            payload = data[offset:offset + e["size"]]
            record = dict(chunk=i, path=path, package=package, export=e["name"],
                          parts=[], n_lods=1, old_format=False, error=None)
            try:
                record.update(read_export(data, offset, payload, slots))
            except Unreadable as ex:
                record["error"] = str(ex)
            out.append(record)
            offset += e["size"]
    return out


def apply(data, wanted):
    """
    `data` with each part's bDisabled set as `wanted` says.

    `wanted` is {flag offset: True/False}. Returns (new bytes, changed count);
    the bytes are the input unchanged when nothing differs, so a caller can tell
    "no edit needed" from "edited" without comparing megabytes.
    """
    changed = 0
    out = bytearray(data)
    for off, off_state in wanted.items():
        now = struct.unpack_from("<I", out, off)[0]
        want = 1 if off_state else 0
        if now != want:
            struct.pack_into("<I", out, off, want)
            changed += 1
    return (bytes(out) if changed else data), changed
