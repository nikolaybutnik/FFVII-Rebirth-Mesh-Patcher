"""
roundtrip.py -- what converting to paks throws away, kept so the way back is
exact.

Converting Dresscode -> loose throws real things away -- the registration
assets, the registry, the pak's exact shape, every original package name.
All of it is small except the packages, and THOSE survive as the paks
themselves. So the conversion stores the rest, compressed, in the
dresscode.json it generates -- and any package no pak carries in
dresscode.bin beside it. Converting back reads both and reproduces the
original mod instead of synthesizing a lookalike. Deleting the key (or
editing the visible fields) simply falls back to a fresh build.
"""
import base64
import hashlib
import json
import os
import struct
import zipfile
import zlib

import conheader
import iostore
import pakfile
import rename
import zen

from formats import analyse
from formats import mkdc
from formats import moddata
from formats import pngfile
from formats import template
from formats import texread

# Where the verbatim package bytes a restore needs are kept. Inside the
# template they were base64 within base64 -- a mod whose paks cannot
# carry every package (costumes for slots no menu row wears, say) made a
# dresscode.json of gigabytes that no editor would open.
SIDECAR = "dresscode.bin"

# A carried parent (see analyse.plan_toggles) rides in an Optional pak under the
# base package's name plus this suffix.
PARENT_ASIDE = "_DCBase"


class BlobFile:
    """The verbatim package bytes a restore needs, written beside
    dresscode.json instead of into it. put() returns the entry number that
    goes in the record; the file is only created once something is put in
    it, so a mod whose paks carry everything gets no sidecar at all."""

    def __init__(self, path):
        self.path = path
        self.zip = None
        self.n = 0

    def put(self, data):
        if self.zip is None:
            self.zip = zipfile.ZipFile(self.path, "w", zipfile.ZIP_STORED)
        # Already zlib'd by the caller, so the zip only has to hold it.
        self.zip.writestr(str(self.n), data)
        self.n += 1
        return self.n - 1

    def close(self):
        if self.zip is not None:
            self.zip.close()


def needs_sidecar(rt):
    """Whether this record keeps its verbatim bytes in the sidecar. Older
    records hold them inline as base64 and need no file."""
    for rec in [rt] + list(rt.get("libraries") or []):
        if any(not isinstance(v, str)
               for v in (rec.get("stored_chunks") or {}).values()):
            return True
    return False


def pack_restore(obj):
    return base64.b64encode(
        zlib.compress(json.dumps(obj).encode("utf-8"), 9)).decode("ascii")


def unpack_restore(text):
    """The decoded record, or None for absent/corrupt -- never an error:
    a mangled record just means a fresh build."""
    if not text:
        return None
    try:
        return json.loads(zlib.decompress(base64.b64decode(text)))
    except Exception:
        return None


def restore_matches(rt, meta, outfits, extras=()):
    """
    True when nothing the person can see was changed since the conversion
    recorded the original -- names, descriptions, pictures. Any edit means
    they WANT something different, so the build honors the edit instead of
    the record.
    """
    vis = rt.get("visible", {})
    if (meta["name"], meta["author"], meta["description"],
            meta["category"], meta["version"]) != \
            (vis.get("name"), vis.get("author"), vis.get("description"),
             vis.get("category"), vis.get("version")):
        return False
    recorded = {rel: tuple(v) for rel, v in vis.get("outfits", {}).items()}
    if {o["folder"] for o in outfits} != set(recorded):
        return False
    for o in outfits:
        if (o["name"], o["description"]) != recorded[o["folder"]]:
            return False

    # Pictures: the file each folder nominates must hash to what was
    # extracted. A swapped, added or deleted picture is an edit.
    def md5_of(path):
        if not path:
            return None
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    by_folder = {}
    for rel, digest in rt.get("pngs", {}).items():
        folder = rel.rsplit("/", 1)[0] if "/" in rel else "."
        by_folder[folder] = digest
    for o in outfits:
        got = md5_of(o.get("preview"))
        want = by_folder.get(o["folder"])
        # A single-outfit mod lives at the root, beside the mod's icon --
        # with no preview of its own, the outfit picks the icon up as its
        # folder's image. Seeing the icon twice is not an edit.
        if got != want and not (want is None and got == rt.get("icon_md5")):
            return False

    # An add-on's picture sits beside its pak instead of in a folder, so it
    # is compared by pak name -- unique across a mod (check_part_names).
    # Only the paks the record knows are weighed: a picture beside some
    # other pak does nothing, and must not read as an edit.
    recorded = rt.get("part_pngs") or {}
    for u in extras:
        stem = os.path.splitext(os.path.basename(u))[0]
        if stem in recorded and md5_of(template.pak_image(u)) != recorded[stem]:
            return False
    return md5_of(meta.get("icon")) == rt.get("icon_md5")


def _entry_row(e):
    """A header entry as the record keeps it. The size flags go last and
    only when set, so a mod without any is recorded exactly as before."""
    row = [e["lo"], e["pad"], e["exp"], e["bun"], e["deps"]]
    return row + [e["flags"]] if e["flags"] else row


def library_record(root, lib_utoc, lib_uplugin, variants, optionals,
                   carried, sink):
    """
    A restore record for a library mod whose packages were inlined into a
    dependent's loose conversion -- the same shape as the dependent's own
    record, sharing the paks as the package source. Its uncarried
    packages (registration assets, helpers nothing referenced) ride as
    verbatim bytes.
    """
    toc = iostore.Toc(lib_utoc)
    packages = rename.read_packages(toc)
    name_of = {pid: p["name"] for pid, p in packages.items()}
    hdr_chunk = next(i for i in range(toc.n) if toc.chunk_ids[i][11] == 10)
    hdr = toc.read(hdr_chunk)
    info = conheader.parse(hdr)
    ids = conheader.package_ids(hdr, info)
    entry_of = {}
    for j, pid in enumerate(ids):
        _sz, exp, bun, lo, pad = struct.unpack_from(
            "<Qiiii", hdr, info["store_off"] + j * 32)
        entry_of[pid] = dict(
            exp=exp, bun=bun, lo=lo, pad=pad, order=j,
            deps=[str(d) for d in
                  conheader.imported_packages(hdr, info, j)],
            flags=conheader.entry_flags(hdr, info, j))
    chunk_order, dir_paths = [], []
    for i in range(toc.n):
        t = toc.chunk_ids[i][11]
        pid = int.from_bytes(toc.chunk_ids[i][:8], "little")
        chunk_order.append(["" if t == 10 else name_of.get(pid, ""), t])
        dir_paths.append(toc.paths.get(i, ""))
    neg_arcs, stored_chunks, stored_bulks = {}, {}, {}
    for pid, p in packages.items():
        data = toc.read(p["chunk"])
        bad = [k for k, pos in enumerate(mkdc.arc_positions(data))
               if struct.unpack_from("<i", data, pos)[0] == -1]
        if bad:
            neg_arcs[p["name"]] = bad
        if p["name"].lower() in carried:
            continue
        stored_chunks[p["name"]] = sink.put(zlib.compress(data, 9))
        blobs = []
        for i in range(toc.n):
            cid = toc.chunk_ids[i]
            if cid[11] in (3, 4) and \
                    int.from_bytes(cid[:8], "little") == pid:
                blobs.append([bytes(cid).hex(),
                              sink.put(zlib.compress(toc.read(i), 9))])
        if blobs:
            stored_bulks[p["name"]] = blobs
    with open(os.path.splitext(lib_utoc)[0] + ".pak", "rb") as f:
        pak_mount, pak_seed, pak_files = pakfile.read_entries(
            f.read(), iostore.oodle_decompress)
    try:
        with open(lib_uplugin, "rb") as f:
            uplugin_raw = f.read()
    except OSError:
        uplugin_raw = b""
    icon_b64 = None
    icon_file = os.path.join(os.path.dirname(lib_uplugin), "Resources",
                             "Icon128.png")
    if os.path.exists(icon_file):
        with open(icon_file, "rb") as f:
            icon_b64 = base64.b64encode(f.read()).decode("ascii")
    record = dict(
        plugin=root,
        mount=toc.mount,
        cid=str(toc.container_id),
        hdr_len=len(hdr),
        uplugin=base64.b64encode(uplugin_raw).decode("ascii"),
        icon_md5=None,
        icon_b64=icon_b64,
        id_order=[name_of.get(pid, "") for pid in ids],
        entries={name_of[pid]: _entry_row(e)
                 for pid, e in entry_of.items() if pid in name_of},
        chunk_order=chunk_order,
        dir_paths=dir_paths,
        neg_arcs=neg_arcs,
        stored_chunks=stored_chunks,
        stored_bulks=stored_bulks,
        pak_mount=pak_mount,
        pak_seed=str(pak_seed),
        pak_files=[[p, base64.b64encode(zlib.compress(b, 9)).decode("ascii")]
                   for p, b in pak_files],
        variants=variants,
        optionals=optionals,
        pngs={},
        visible={},
    )
    toc.close()
    return record


def record_roundtrip(toc, uplugin, plugin, plans, ctx, mod_out, layout,
                     opt_layout=(), gun_layout=(), orig=None, libraries=()):
    """
    Leave everything beside the written paks that converting BACK will
    need: a prefilled dresscode.json, each outfit's preview as a PNG a person
    can see and swap, the plugin icon -- and the opaque restore record.

    `layout` is [(rel_folder, plan)], "." meaning the mod's root folder;
    `opt_layout` is [(folder, pak stem, toggle)] for the generated Optional
    paks, and `gun_layout` [(folder, pak stem, preview ref)] for the
    weapons-menu tiles handed back as override paks.

    When library mods were inlined, `toc` is the MERGED container the plans
    were made against, `orig` the dependent's own, and `libraries` is
    [(root, utoc, uplugin)] per library. The main record then takes its
    container SHAPE from the original and covers only its own packages;
    each library gets a sibling record of the same form, so the way back
    can rebuild every mod exactly as downloaded.
    """
    shape_toc = orig or toc
    packages = ctx["packages"]
    roots = ctx.get("roots", (plugin,))
    by_name = {p["name"].lower(): pid for pid, p in packages.items()}
    md = moddata.read_mod_metadata(toc.read(ctx["assets"]["metadata"])) \
        if "metadata" in ctx["assets"] else {}
    try:
        with open(uplugin, "rb") as f:
            uplugin_raw = f.read()
        up = json.loads(uplugin_raw.decode("utf-8-sig"))
    except Exception:
        uplugin_raw, up = b"", {}

    # --- pictures: extract what can be shown, remember what was written ---
    pngs = {}

    def put_png(folder, stem, chunk):
        """The picture's md5, or None when the texture is in a format
        texread cannot read."""
        got = texread.extract(toc.read(chunk))
        if not got:
            return None
        w, h, bgra = got
        data = pngfile.encode(w, h, bgra)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, stem + ".png")
        with open(path, "wb") as f:
            f.write(data)
        rel = os.path.relpath(path, mod_out).replace("\\", "/")
        pngs[rel] = hashlib.md5(data).hexdigest()
        return pngs[rel]

    for rel, (outfit, *_rest) in layout:
        ref = outfit.get("preview_image")
        chunk = None
        if ref:
            pid = by_name.get(ref.split(".")[0].lower())
            chunk = packages[pid]["chunk"] if pid in packages else None
        if chunk is not None:
            put_png(mod_out if rel == "." else os.path.join(mod_out, rel),
                    "preview", chunk)
    # An add-on's picture goes beside its pak, under the pak's own name,
    # which is where a rebuild looks for it. Recorded even when there is
    # none, so adding one later reads as the edit it is.
    part_pngs = {}
    for rel, stem, ref in ([(r, s, t["row"].get("preview_image"))
                            for r, s, t in opt_layout] + list(gun_layout)):
        pid = by_name.get(ref.split(".")[0].lower()) if ref else None
        part_pngs[stem] = (
            put_png(os.path.join(mod_out, *rel.split("/")), stem,
                    packages[pid]["chunk"]) if pid in packages else None)

    icon_md5 = None
    icon_file = os.path.join(os.path.dirname(uplugin), "Resources",
                             "Icon128.png")
    if os.path.exists(icon_file):
        with open(icon_file, "rb") as f:
            icon_data = f.read()
        with open(os.path.join(mod_out, "icon.png"), "wb") as f:
            f.write(icon_data)
        icon_md5 = hashlib.md5(icon_data).hexdigest()

    # --- the original container's shape (the dependent's own when plans
    # were made against a merged container) ---
    hdr_chunk = next(i for i in range(shape_toc.n)
                     if shape_toc.chunk_ids[i][11] == 10)
    hdr = shape_toc.read(hdr_chunk)
    info = conheader.parse(hdr)
    ids = conheader.package_ids(hdr, info)
    main_pids = set(ids)
    entry_of = {}
    for j, pid in enumerate(ids):
        _sz, exp, bun, lo, pad = struct.unpack_from(
            "<Qiiii", hdr, info["store_off"] + j * 32)
        entry_of[pid] = dict(
            exp=exp, bun=bun, lo=lo, pad=pad, order=j,
            deps=[str(d) for d in
                  conheader.imported_packages(hdr, info, j)],
            flags=conheader.entry_flags(hdr, info, j))

    name_of = {pid: p["name"] for pid, p in packages.items()}
    chunk_order = []
    for i in range(shape_toc.n):
        t = shape_toc.chunk_ids[i][11]
        pid = int.from_bytes(shape_toc.chunk_ids[i][:8], "little")
        chunk_order.append(["" if t == 10 else name_of.get(pid, ""), t])
    # The directory index VERBATIM, aligned with chunk_order. Deriving
    # paths from package names loses the cooker's file-name casing (a
    # package whose name table says "pink" can sit in the index as
    # "Pink.uasset"), and a restore must not.
    dir_paths = [shape_toc.paths.get(i, "") for i in range(shape_toc.n)]

    # Which graph arcs are -1 in the ORIGINAL. The loose conversion must
    # flatten them to 0 (the ~mods loader refuses them); converting back
    # puts them back by ordinal.
    neg_arcs = {}
    for pid, p in packages.items():
        if pid not in main_pids:
            continue                    # a library's; its own record covers it
        data = toc.read(p["chunk"])
        bad = [k for k, pos in enumerate(mkdc.arc_positions(data))
               if struct.unpack_from("<i", data, pos)[0] == -1]
        if bad:
            neg_arcs[p["name"]] = bad

    # The registration assets plus the toggle blueprints and material packs
    # travel as verbatim bytes -- they are Dresscode-only machinery no loose
    # pak may carry. So does any Optional-pak package whose name table
    # mentions an overridden base package: the override collapses two names
    # into one, and no inverse map can pull them apart again.
    # The folder is normally made by the first preview written into it;
    # a weapon mod has no preview, and the sidecar must land somewhere.
    os.makedirs(mod_out, exist_ok=True)
    sink = BlobFile(os.path.join(mod_out, SIDECAR))
    stored_pids = {int.from_bytes(toc.chunk_ids[c][:8], "little")
                   for c in ctx["assets"].values()}
    stored_pids |= ctx.get("blob_only", set())
    for _rel, _stem, t in opt_layout:
        overridden = {b.lower() for b in t["overrides"]}
        for pid in t["keep"]:
            z = zen.ZenPackage(toc.read(packages[pid]["chunk"]))
            if any(n.lower() in overridden for n in z.names):
                stored_pids.add(pid)
    stored_chunks = {
        name_of[pid]: sink.put(
            zlib.compress(toc.read(packages[pid]["chunk"]), 9))
        for pid in stored_pids if pid in packages and pid in main_pids}

    # The completeness net: anything neither carried by a pak nor
    # stored above would be unrecoverable. Seen in the wild: alternative
    # hair meshes only a toggle actor references, spare icon textures.
    # Their bulk data rides too -- a mesh has .ubulk.
    carried = set()
    for _rel, (_o, _t, _ren, _obj, drop) in layout:
        dropped = {d.lower() for d in (drop or ())}
        carried |= {p["name"].lower() for p in packages.values()} - dropped
    stored_bulks = {}
    for pid, p in packages.items():
        if pid not in main_pids or p["name"].lower() in carried \
                or name_of[pid] in stored_chunks:
            continue
        stored_chunks[name_of[pid]] = sink.put(
            zlib.compress(toc.read(p["chunk"]), 9))
        blobs = []
        for i in range(toc.n):
            cid = toc.chunk_ids[i]
            if cid[11] in (3, 4) and \
                    int.from_bytes(cid[:8], "little") == pid:
                blobs.append([bytes(cid).hex(),
                              sink.put(zlib.compress(toc.read(i), 9))])
        if blobs:
            stored_bulks[name_of[pid]] = blobs

    # --- the original pak, entry by entry, decompressed ---
    loose_path = os.path.splitext(shape_toc.path)[0] + ".pak"
    with open(loose_path, "rb") as f:
        pak_mount, pak_seed, pak_files = pakfile.read_entries(
            f.read(), iostore.oodle_decompress)

    # --- inverse rename maps, per variant. Keys in `renames` are lowercased;
    # the restore must write back EXACT original strings, so recover casing
    # from the packages themselves and, for external condition meshes, from
    # the mesh's own name table.
    case_of = {p["name"].lower(): p["name"] for p in packages.values()}
    own = set(case_of)
    variants = {}
    for rel, (outfit, target, renames, objects, _drop) in layout:
        mesh_pid = by_name.get(outfit["skeletal_mesh"].split(".")[0].lower())
        if mesh_pid is not None:
            for cond in analyse.condition_refs(toc, packages[mesh_pid]["chunk"], own):
                case_of.setdefault(cond.lower(), cond)
        back = {new.lower(): case_of.get(old, old)
                for old, new in renames.items() if old != new.lower()}
        objects_back = {}
        for pkg_low, m in objects.items():
            if not m:
                continue
            new_pkg = renames.get(pkg_low, pkg_low)
            objects_back[new_pkg.lower()] = {v: k for k, v in m.items()}
        variants[rel] = dict(renames_back=back, objects_back=objects_back)

    # The Optional paks hold packages under their OVERRIDE names; the way
    # back needs each one's true name again -- and the full inverse map,
    # because kept materials also NAME dropped packages in their strings.
    # A mesh-carrying optional used its base variant's renames wholesale,
    # so its inverse is that variant's inverse.
    rel_of_mesh = {}
    for rel, (o, *_r) in layout:
        pid = by_name.get(o["skeletal_mesh"].split(".")[0].lower())
        if pid is not None:
            rel_of_mesh[pid] = rel
    # Keyed by folder so a person reading the record can tell what is what,
    # but the pak's own name is what finds it again: folders get renamed and
    # moved, generated file names do not.
    optionals = {}
    for opt_rel, stem, t in opt_layout:
        if t["kind"] == "mesh":
            v = variants[rel_of_mesh[t["mesh_pid"]]]
            back = dict(v["renames_back"])
            objects_back = {k: dict(m) for k, m in v["objects_back"].items()}
            # Overridden paths hold the REPLACEMENT in this pak.
            rel_v = rel_of_mesh[t["mesh_pid"]]
            plan_renames = next(p[2] for r, p in layout if r == rel_v)
            for base_pkg, repl_pkg in t["overrides"].items():
                new = plan_renames.get(
                    base_pkg.lower(),
                    analyse.converted_name(base_pkg, roots, t["costume_root"]))
                back[new.lower()] = repl_pkg
            for repl_low, m in t["objects"].items():
                new_pkg = next((k for k, vv in back.items()
                                if vv.lower() == repl_low), None)
                if new_pkg:
                    objects_back.setdefault(new_pkg, {}).update(
                        {v2: k2 for k2, v2 in m.items()})
            optionals[opt_rel] = dict(pak=stem, renames_back=back,
                                      objects_back=objects_back)
            continue
        back = {analyse.converted_name(p["name"], roots,
                               t["costume_root"]).lower(): p["name"]
                for p in packages.values()}
        for base_pkg, repl_pkg in t["overrides"].items():
            back[analyse.converted_name(base_pkg, roots,
                                t["costume_root"]).lower()] = repl_pkg
        for base_pkg in t["carry"]:
            back[(analyse.converted_name(base_pkg, roots, t["costume_root"])
                  + PARENT_ASIDE).lower()] = base_pkg
        back = {k: v for k, v in back.items() if k != v.lower()}
        objects_back = {}
        for repl_low, m in t["objects"].items():
            new_pkg = next((k for k, v in back.items()
                            if v.lower() == repl_low), None)
            if new_pkg:
                objects_back[new_pkg] = {v: k for k, v in m.items()}
        optionals[opt_rel] = dict(pak=stem, renames_back=back,
                                  objects_back=objects_back)

    # Snapshot what the template will SHOW (empty names fall back to the
    # folder), or the untouched-template check can never match.
    mod_name = md.get("friendly_name") or plugin
    visible = dict(
        name=mod_name,
        author=md.get("created_by", ""),
        description=md.get("description", ""),
        category=md.get("category") or "Outfit",
        version=up.get("VersionName") or "1.0.0",
        outfits={rel: [o["name"] or (mod_name if rel == "." else rel),
                       o["description"]]
                 for rel, (o, *_r) in layout},
    )

    lib_records = [library_record(root, lib_utoc, lib_up, variants,
                                  optionals, carried, sink)
                   for root, lib_utoc, lib_up in libraries]
    sink.close()
    record = dict(
        plugin=plugin,
        mount=shape_toc.mount,
        cid=str(shape_toc.container_id),
        hdr_len=len(hdr),
        uplugin=base64.b64encode(uplugin_raw).decode("ascii"),
        icon_md5=icon_md5,
        id_order=[name_of.get(pid, "") for pid in ids],
        entries={name_of[pid]: _entry_row(e)
                 for pid, e in entry_of.items() if pid in name_of},
        chunk_order=chunk_order,
        dir_paths=dir_paths,
        neg_arcs=neg_arcs,
        stored_chunks=stored_chunks,
        stored_bulks=stored_bulks,
        pak_mount=pak_mount,
        pak_seed=str(pak_seed),
        pak_files=[[p, base64.b64encode(zlib.compress(b, 9)).decode("ascii")]
                   for p, b in pak_files],
        variants=variants,
        optionals=optionals,
        pngs=pngs,
        part_pngs=part_pngs,
        visible=visible,
        libraries=lib_records,
    )

    parts = [(rel, None) for rel, _plan in layout]
    # Normally an outfit's preview has made this folder already; a weapon
    # mod has no outfit and no preview of its own.
    os.makedirs(mod_out, exist_ok=True)
    template.write_template(mod_out, visible["name"], parts,
                   prefill=dict(name=visible["name"],
                                author=visible["author"],
                                description=visible["description"],
                                category=visible["category"],
                                version=visible["version"],
                                outfits={rel: tuple(v) for rel, v in
                                         visible["outfits"].items()}),
                   restore=pack_restore(record),
                   sidecar=SIDECAR if sink.n else None,
                   extras=[(rel.split("/")[-1], str(v.get("pak", "")))
                           for rel, v in optionals.items()])
    print(f"    recorded  {template.TEMPLATE}"
          + (f" + {SIDECAR}" if sink.n else "")
          + " + pictures -- converting the folder back restores this "
            "mod exactly")
    return 0
