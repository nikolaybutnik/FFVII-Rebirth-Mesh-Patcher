"""
toggles.py -- turns the old modular standard's optional paks into Dresscode
toggles.

Before Dresscode, a mod hid a part by dropping a tiny pak next to the base
one: it overrode a package the outfit used -- usually the mask TEXTURE a
material samples -- and won on load order. Dresscode instead swaps whole
MATERIALS on named mesh slots, driven by a blueprint holding an
EndMaterialPack.

Bridging the two means following the chain the optional implies:

    overridden package -> the outfit materials that sample it
                       -> the mesh slots those materials sit on

and then giving each affected material a private copy that samples the
optional's version instead. The copies, the pack and the blueprint all live
in one folder per toggle, so nothing an optional does can leak into the
outfit it came from.
"""

import os

import cityhash
import conheader
import pkgedit
import matpack
import mkbp
import rename
from zen import ZenPackage

ENDGAME = "/Script/EndGame"

# Bigger than any material or mask; a mesh is megabytes and never a candidate.
SMALL = 200_000


def leaf(package_name):
    return package_name.rsplit("/", 1)[-1]


def label_of(utoc):
    """A readable toggle name from a pak file name: the modular standard's
    sort prefix and the _P suffix are plumbing, not words."""
    stem = os.path.splitext(os.path.basename(utoc))[0]
    if stem.lower().endswith("_p"):
        stem = stem[:-2]
    parts = stem.split("_")
    if parts and parts[0][:1].isdigit():
        parts = parts[1:]
    return " ".join(parts).strip() or stem


def header_deps(toc):
    """{package id -> [imported package ids]} from a container's header."""
    for i in range(toc.n):
        if toc.chunk_ids[i][11] != 10:
            continue
        hdr = toc.read(i)
        info = conheader.parse(hdr)
        if not info:
            break
        ids = conheader.package_ids(hdr, info)
        return {pid: conheader.imported_packages(hdr, info, k)
                for k, pid in enumerate(ids)}
    return {}


def references(toc, packages):
    """{package path -> [packages naming it]}, over the small packages only --
    materials and masks are all that can matter, and a mesh is enormous."""
    out = {}
    for pkg in packages.values():
        data = toc.read(pkg["chunk"])
        if len(data) > SMALL:
            continue
        for n in ZenPackage(data).names:
            if n.startswith("/") and n != pkg["name"]:
                out.setdefault(n.lower(), []).append(pkg["name"])
    return out


def export_index(toc, packages):
    """
    {export object ID -> (source package, object path)} for a container.

    An object's ID hashes its package's path, so moving a package changes the
    ID of everything in it. A package rewritten ALONE -- a private material
    copy -- has no way to know the new IDs of the textures it samples, and
    an import that resolves to nothing simply renders nothing. This index is
    what lets those be rewritten too.

    EVERY package counts. A 100 MB texture with inline mips is imported by
    materials all the same -- a size filter that once skipped "big things
    nothing imports" left recolor copies sampling a path that no longer
    existed, and whole toggles rendered grey.
    """
    out = {}
    for pkg in packages.values():
        z = ZenPackage(toc.read(pkg["chunk"]))
        source = pkgedit.source_name_of(z)
        for e in z.exports:
            path = pkgedit.export_object_path(z, e)
            out[cityhash.object_id(source, path)] = (source, path)
    return out


def moved_imports(index, table):
    """{old object ID -> new object ID} for every indexed export whose
    package `table` renames."""
    out = {}
    for oid, (source, path) in index.items():
        new = table.get(source.lower())
        if new:
            out[oid] = cityhash.object_id(new, path)
    return out


def slot_materials(toc, mesh_chunk):
    """{slot name -> (material package, material object)} for a mesh."""
    resolve = matpack.object_resolver(toc)
    out = {}
    for slot, imp, _off in matpack.material_slots(toc.read(mesh_chunk)):
        target = resolve.get(imp)
        if target:
            out[slot] = target
    return out


def plan(toc, packages, mesh_chunk, parts, refs=None):
    """
    What one variant does to one outfit: which materials its parts change
    the look of, and which mesh slots those materials occupy. `parts` is
    [(toc, packages)], one entry per override pak -- a variant built from
    several stacks them all.

    Returns (slots, overridden) -- slots is {slot: (material package, object)}
    naming the outfit's ORIGINAL material for each affected slot.
    """
    refs = references(toc, packages) if refs is None else refs
    slots = slot_materials(toc, mesh_chunk)
    overridden = {p["name"] for _etoc, epkgs in parts
                  for p in epkgs.values()}
    low = {n.lower() for n in overridden}

    affected = set()
    for name in overridden:
        affected.update(refs.get(name.lower(), []))
        affected.add(name)                  # it may be a material itself
    hit = {slot: mat for slot, mat in slots.items()
           if mat[0] in affected or mat[0].lower() in low}
    return hit, overridden


def emit(toc, packages, parts, slots, renames, plugin,
         safe_outfit, safe_extra, mesh_package, mesh_object, index=None,
         parts_index=None):
    """
    Every package one variant needs, and the soft path of the actor that
    drives it. `slots` comes from plan(), `renames` is the outfit's own map
    from stock paths into the plugin. `parts` come in the user's order;
    where two override the same package the LATER wins -- the rule load
    order gave the old standard's paks.

    Returns ([{name, data, deps, bulks}], actor path).
    """
    base = f"/{plugin}/Extras/{safe_outfit}/{safe_extra}"

    winner = {}                             # low name -> owning (toc, pid, pkg)
    for etoc, epkgs in parts:
        for pid, pkg in epkgs.items():
            winner[pkg["name"].lower()] = (etoc, pid, pkg)
    moved = dict(renames)
    taken = {}
    for low in sorted(winner):
        name = winner[low][2]["name"]
        # Two parts may bring same-leaf packages from different folders;
        # they must not land on one target.
        tgt, n = f"{base}/{leaf(name)}", 1
        while taken.get(tgt.lower(), low) != low:
            n += 1
            tgt = f"{base}/{leaf(name)}{n}"
        taken[tgt.lower()] = low
        moved[low] = tgt

    def remap(pids, table):
        by_id = {cityhash.package_id(old): cityhash.package_id(new)
                 for old, new in table.items()}
        return [by_id.get(p, p) for p in pids]

    index = export_index(toc, packages) if index is None else index
    # Cross-pak references -- one part's material sampling another part's
    # mask -- remap by object ID, so every part's exports are indexed.
    ex_index = dict(index)
    if parts_index is None:
        for etoc, epkgs in parts:
            ex_index.update(export_index(etoc, epkgs))
    else:
        ex_index.update(parts_index)

    out, emitted = [], set()
    # Each part's own packages -- the alternate masks and materials, with
    # any bulk data (a mask big enough to stream keeps its .ubulk).
    for etoc, epkgs in parts:
        mine = {pid: pkg for pid, pkg in epkgs.items()
                if winner[pkg["name"].lower()][0] is etoc
                and pkg["name"].lower() not in emitted}
        if not mine:
            continue
        emitted.update(p["name"].lower() for p in mine.values())
        deps_extra = header_deps(etoc)
        new_data, new_ids = rename.rewrite_chunks(
            etoc, mine, moved, fix_arcs=True,
            extra_imports=moved_imports(ex_index, moved))
        bulks = {}
        for i in range(etoc.n):
            if etoc.chunk_ids[i][11] not in (3, 4):
                continue
            cid12 = new_ids.get(i, etoc.chunk_ids[i])
            bulks.setdefault(int.from_bytes(cid12[:8], "little"), []).append(
                (cid12, new_data.get(i) or etoc.read(i)))
        for pid, pkg in mine.items():
            data = new_data.get(pkg["chunk"]) or etoc.read(pkg["chunk"])
            new_name = moved[pkg["name"].lower()]
            out.append(dict(name=new_name, data=data,
                            deps=remap(deps_extra.get(pid, []), moved),
                            bulks=bulks.get(cityhash.package_id(new_name),
                                            [])))

    # A material no part itself replaces needs a private copy, identical
    # but sampling the variant's masks.
    deps_main = header_deps(toc)
    by_name = {p["name"].lower(): (pid, p) for pid, p in packages.items()}
    for _slot, (mat_pkg, _obj) in sorted(slots.items()):
        low = mat_pkg.lower()
        if low in winner:
            continue
        if low not in by_name:
            continue                        # a stock material, not ours to copy
        pid, pkg = by_name[low]
        copy_to = dict(moved)
        copy_to[low] = f"{base}/{leaf(mat_pkg)}"
        copied, _ids2 = rename.rewrite_chunks(
            toc, {pid: pkg}, copy_to, fix_arcs=True,
            extra_imports=moved_imports(ex_index, copy_to))
        data = copied.get(pkg["chunk"]) or toc.read(pkg["chunk"])
        out.append(dict(name=copy_to[low], data=data,
                        deps=remap(deps_main.get(pid, []), copy_to), bulks=[]))

    entries, mats = [], set()
    for slot, (mat_pkg, obj) in sorted(slots.items()):
        target = f"{base}/{leaf(mat_pkg)}"
        entries.append((slot, target, obj))
        mats.add(target)

    pack_name = f"{base}/{safe_extra}_MP"
    out.append(dict(
        name=pack_name,
        data=mkbp.material_pack(pack_name, f"{safe_extra}_MP", entries),
        deps=sorted(cityhash.package_id(m) for m in mats), bulks=[]))

    bp_name = f"{base}/{safe_extra}"
    out.append(dict(
        name=bp_name,
        data=mkbp.toggle_actor(bp_name, f"{safe_extra}_C", pack_name,
                               f"{safe_extra}_MP", mesh_package, mesh_object),
        deps=sorted({cityhash.package_id(pack_name),
                     cityhash.package_id(mesh_package)}), bulks=[]))

    return out, f"{bp_name}.{safe_extra}"
