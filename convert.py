"""
convert.py -- converts a FFVII Rebirth costume mod between its two formats:

    DRESSCODE   picked in the Dresscode menu
    PAK         dropped in ~mods, always worn

    python convert.py "D:\\mods\\Some Mod"

Or just drag a mod onto convert.py -- a folder, several at once, or a
.zip/.7z/.rar. It works out which format each mod is in, says what it will
make, asks before writing, and puts the result beside the original.
Originals are never touched.
"""
# HOW IT WORKS (not printed -- the docstring above doubles as the usage text)
# ---------------------------------------------------------------------------
# DRESSCODE   A plugin folder: <Mod>.uplugin and a container under
#             Content/Paks/WindowsNoEditor, packages named /<Mod>/...
# PAK         A .utoc/.ucas/.pak in ~mods, packages named /Game/..., winning
#             by overriding a stock costume.
#
# A conversion renames the MESH package onto the stock costume it replaces
# and moves every other package into that costume's folder under /Game/, plus
# a new .pak carrying the mount point the new format expects. lib/rename.py
# does the renames, lib/pakfile.py the pak.
#
# The /Game/ move is NOT cosmetic. Packages are found by ID, but the async
# loader silently skips imports into a root that is not mounted -- and a
# ~mods container mounts no plugin root. Probed in game: a mesh whose
# materials kept their /<Mod>/... names loads and renders, with every such
# material slot NULL (the default checker) while its /Game/ slots resolve.
#
# Deliberately NOT here: game detection, a mod library, in-place editing.
# Conversion only ever creates (the one exception: the pre-1.005 pre-flight
# can offer to run patch.py on the source, which keeps its usual backups).

import base64
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import cityhash                                                 # noqa: E402
import conheader                                                # noqa: E402
import drops                                                    # noqa: E402
import iostore                                                  # noqa: E402
import loosepak                                                 # noqa: E402
import matpack                                                  # noqa: E402
import pakfile                                                  # noqa: E402
import rename                                                   # noqa: E402
import slots                                                    # noqa: E402
import stockgraft                                               # noqa: E402
import zen                                                      # noqa: E402
from formats import analyse                                     # noqa: E402
from formats import mkdc                                        # noqa: E402
from formats import moddata                                     # noqa: E402
from formats import pngfile                                     # noqa: E402
from formats import template                                    # noqa: E402
from formats import texread                                     # noqa: E402
from formats import toggles                                     # noqa: E402
from formats import weapons                                     # noqa: E402

# Where the verbatim package bytes a restore needs are kept. Inside the
# template they were base64 within base64 -- a mod whose paks cannot
# carry every package (costumes for slots no menu row wears, say) made a
# dresscode.json of gigabytes that no editor would open.
SIDECAR = "dresscode.bin"

VARIANTS_DIR_OUT = "Variants"           # what this tool writes

# A carried parent (see plan_toggles) rides in an Optional pak under the
# base package's name plus this suffix.
PARENT_ASIDE = "_DCBase"


# ---------------------------------------------------------------------------
# The round-trip record. Converting Dresscode -> loose throws real things
# away -- the registration assets, the registry, the pak's exact shape, every
# original package name. All of it is small except the packages, and THOSE
# survive as the paks themselves. So the conversion stores the rest,
# compressed, in the dresscode.json it generates -- and any package no pak
# carries in dresscode.bin beside it. Converting back reads both and
# reproduces the original mod instead of synthesizing a lookalike.
# Deleting the key (or editing the visible fields) simply falls back to a
# fresh build.
# ---------------------------------------------------------------------------

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
                  conheader.imported_packages(hdr, info, j)])
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
        entries={name_of[pid]: [e["lo"], e["pad"], e["exp"], e["bun"],
                                e["deps"]]
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
                  conheader.imported_packages(hdr, info, j)])

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
        entries={name_of[pid]: [e["lo"], e["pad"], e["exp"], e["bun"],
                                e["deps"]]
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


def _within(child, parent):
    """True when `parent` is `child` itself or a folder above it."""
    child = os.path.normcase(os.path.abspath(child))
    parent = os.path.normcase(os.path.abspath(parent))
    return child == parent or child.startswith(parent + os.sep)


def retouches(outfits, pkgs):
    """
    Whether this pak is a stock-texture retouch belonging to one of these
    outfits -- it overrides game packages the outfit's STOCK materials
    sample (skin, head), which is how mods shipped before the modular
    standard: mesh in one pak, retouched stock textures in another.

    Nothing in the mod records the link; only the game's own files show it,
    so the walk that would carry those materials is what decides.
    """
    pids = set(pkgs)
    for _utoc, opkgs, hdeps, mesh_pid in outfits:
        if mesh_pid is None:
            continue
        merged = dict(opkgs)
        merged.update(pkgs)
        meta = {pid: (1, 1, hdeps.get(pid, ())) for pid in merged}
        if stockgraft.links(merged, meta, {mesh_pid}) & pids:
            return True
    return False


def classify_companions(outfit_utocs, candidates):
    """
    ({outfit utoc: [companion utoc]}, options) for non-outfit paks living
    OUTSIDE an Optional folder.

    A REQUIRED companion is one the outfit's packages hard-import from and
    that overrides nothing of theirs -- or a stock-texture retouch, which
    imports nothing and is recognised by retouches() instead.

    A companion belongs to the outfit it ships beside: one in the outfit's
    own folder, or in a folder above it (a pak shared by every outfit). A
    mod offering several versions repeats one pak name in each version's
    folder with DIFFERENT bytes, so a sibling's copy must not reach here.

    Everything else is an option: it overrides outfit packages (an old-style
    mask swap), or overrides a rival in the same scope (a choose-one set --
    the first in load order is the baked default, the rest convert as
    variants), or touches nothing of the mod's at all (a weapon override
    riding in the same download -- the variant machinery skips it with a
    note).
    """
    own, deps, outfits = set(), set(), []
    total = len(outfit_utocs) + len(candidates)
    for k, u in enumerate(outfit_utocs):
        drops.scanning("sorting companions from options", k, total)
        toc = iostore.Toc(u)
        pkgs = rename.read_packages(toc)
        hdeps = analyse.header_deps(toc)
        mesh, _player = mkdc.find_stock_mesh(pkgs)
        toc.close()
        own |= set(pkgs)
        for pids in hdeps.values():
            deps.update(pids)
        outfits.append((u, pkgs, hdeps,
                        cityhash.package_id(mesh) if mesh else None))
    infos = []
    for k, u in enumerate(candidates):
        drops.scanning("sorting companions from options",
                 len(outfit_utocs) + k, total)
        toc = iostore.Toc(u)
        bulk_pids = {int.from_bytes(toc.chunk_ids[i][:8], "little")
                     for i in range(toc.n) if toc.chunk_ids[i][11] in (3, 4)}
        infos.append((u, rename.read_packages(toc), bulk_pids))
        toc.close()
    drops.scanning("sorting companions from options", total, total)

    options, kind = [], []
    for u, pkgs, bulk_pids in infos:
        pids = set(pkgs)
        if pids & own:
            options.append(u)               # overrides the outfit itself
        elif pids & deps or retouches(outfits, pkgs):
            kind.append((u, pids))
        elif not pids and bulk_pids & own:
            # No packages at all, only bulk data belonging to the outfit's
            # own: the separate "optional" pak of top texture mips. It is
            # part of the outfit, never a thing to choose in a menu.
            kind.append((u, bulk_pids))
        else:
            options.append(u)               # nothing of this mod's at all
    # Load order settled choose-one sets in ~mods; the alphabetical first is
    # the default the others were alternatives to. Rivalry is judged per
    # outfit -- two VERSIONS' copies of one pak are not rivals.
    kind.sort(key=lambda t: os.path.basename(t[0]).lower())
    comp_map, used = {}, set()
    for utoc, _pkgs, _hdeps, _mesh in outfits:
        home = os.path.dirname(utoc)
        taken, mine = set(), []
        for u, pids in kind:
            if not _within(home, os.path.dirname(u)) or pids & taken:
                continue
            mine.append(u)
            taken |= pids
            used.add(u)
        comp_map[utoc] = mine
    # A download can keep the shared files in a branch of their own -- one
    # "textures" folder beside the costume folders -- so no outfit's folder
    # scope claims them and they used to fall through to the options list.
    # The costume then converted with nothing to skin it: every mesh
    # hard-imports those materials and the game has no such file, so the
    # tile came out bare (field report). They belong to every outfit.
    # Several paks defining the same packages are a choose-one set, and load
    # order settled those in ~mods -- the first is the one that gets baked
    # in. All of them stay selectable as add-ons on top.
    baked = set()
    for u, pids in kind:
        if u in used or not (pids & deps) or pids & baked:
            continue
        baked |= pids
        for utoc in comp_map:
            comp_map[utoc].append(u)
    options += [u for u, _pids in kind if u not in used]
    return comp_map, sorted(options)


def stack_tree(outfits, extras):
    """
    The shared override tree a stackable build leaves at /Game/ paths: every
    package an extra pak overrides, closed over what those packages import
    from the mod itself (an extra served from ~mods keeps its original
    imports, so whatever it names must stay at /Game/ too). Lowercase names.
    """
    ext, frontier = set(), []
    for utoc in extras:
        toc = iostore.Toc(utoc)
        for p in rename.read_packages(toc).values():
            ext.add(p["name"].lower())
            frontier += [n.lower() for n in
                         zen.ZenPackage(toc.read(p["chunk"])).names
                         if n.startswith("/")]
        toc.close()
    tocs, base = [], {}
    for o in outfits:
        toc = iostore.Toc(o["utoc"])
        tocs.append(toc)
        for p in rename.read_packages(toc).values():
            base.setdefault(p["name"].lower(), (toc, p))
    for n in sorted(ext):               # the base copies' imports count too
        if n in base:
            toc, p = base[n]
            frontier += [m.lower() for m in
                         zen.ZenPackage(toc.read(p["chunk"])).names
                         if m.startswith("/")]
    while frontier:
        n = frontier.pop()
        if n in ext or n not in base:   # not ours -> a stock import, fine
            continue
        ext.add(n)
        toc, p = base[n]
        frontier += [m.lower() for m in
                     zen.ZenPackage(toc.read(p["chunk"])).names
                     if m.startswith("/")]
    for toc in tocs:
        toc.close()
    return ext


def write_masks_pak(outfit_utoc, ext, out_dir, base_name):
    """The ~mods pak serving a stackable build's override tree: the base
    pak's copies of `ext`, names untouched. Mounted at the content root --
    the tree may reach outside Character/Player (a mod overriding a common
    skin detail does). Returns the .utoc path."""
    def content_path(package_name):
        if not package_name.lower().startswith("/game/"):
            raise RuntimeError(
                f"cannot place {package_name} in a pak")
        return package_name[len("/Game/"):]

    toc = iostore.Toc(outfit_utoc)
    packages = rename.read_packages(toc)
    drop = [p["name"] for p in packages.values()
            if p["name"].lower() not in ext]
    os.makedirs(out_dir, exist_ok=True)
    written = rename.rename_container(
        toc, {}, "../../../End/Content/", content_path, out_dir, base_name,
        container_name=base_name, drop=drop, fix_arcs=True, cross_pak=True,
        quiet=True)
    with open(os.path.join(out_dir, base_name + ".pak"), "wb") as f:
        f.write(pakfile.build(pakfile.LOOSE_MOUNT))
    toc.close()
    return written


def merge_loose(utocs, out_dir, base, extra=None):
    """
    One loose container carrying every package of `utocs` -- an outfit that
    ships as several REQUIRED paks (the mesh in one, its materials and
    textures in another) becomes a single container the conversion treats
    as THE outfit. Package bytes, names, arcs and bulk data are carried
    unchanged; only the container header and directory are new. The first
    pak wins a package two of them carry. `extra` is {pid: record} for
    packages from elsewhere -- the game's own, say -- added after the paks'.
    Returns the merged .utoc path.
    """
    tocs = [iostore.Toc(u) for u in utocs]
    template = tocs[0]
    merged, order = {}, []
    # Bulk data for one package can live in a DIFFERENT pak: authors ship the
    # top texture mips as a separate "optional" container of .uptnl chunks
    # with no packages of its own. Collect across every pak first, or the
    # merge keeps only what sat beside the .uasset and the mod loses detail.
    bulks_all, seen_chunks = {}, set()
    for toc in tocs:
        for i in range(toc.n):
            if toc.chunk_ids[i][11] not in (3, 4):
                continue
            cid12 = bytes(toc.chunk_ids[i])
            if cid12 in seen_chunks:
                continue
            seen_chunks.add(cid12)
            pid = int.from_bytes(cid12[:8], "little")
            bulks_all.setdefault(pid, []).append((toc, i))
    for toc in tocs:
        packages = rename.read_packages(toc)
        if not packages:
            continue        # a mips-only pak: its chunks are in bulks_all
        entry_meta = conheader.store_meta(toc, packages)
        for pid, pkg in packages.items():
            if pid in merged:
                continue
            exp, bun, deps = entry_meta.get(pid, (1, 1, []))
            # Plugin-rooted names occur when merging Dresscode containers
            # with their libraries; the merged container is a conversion
            # INPUT only, so its index paths just have to be self-consistent.
            merged[pid] = dict(
                name=pkg["name"], data=toc.read(pkg["chunk"]),
                exp=exp, bun=bun, deps=list(deps),
                bulks=[loosepak.copied(bt, i, template=template)
                       for bt, i in bulks_all.get(pid, [])])
            order.append(pid)
    for pid, rec in (extra or {}).items():
        if pid not in merged:
            merged[pid] = rec
            order.append(pid)

    written = loosepak.write(order, merged, out_dir, base, template)
    for toc in tocs:
        toc.close()
    return written


def preflight_meshes(utocs, assume_yes, source_hint):
    """
    Pre-V1.005 meshes crash the game the moment they load, and a conversion
    carries meshes exactly as they are. Catch that BEFORE converting -- the
    classic mistake is converting a mod fresh from an archive that predates
    the patch -- and offer to run the patcher right here, backups kept,
    same as patch.py --path. Under --yes there is nobody to ask, so it
    warns and converts as-is: automated runs must stay byte-faithful.
    """
    import patch as patcher
    stale = []
    for k, u in enumerate(utocs):
        drops.scanning("checking meshes", k, len(utocs))
        try:
            state, _n, _msg = patcher.mod_status(u)
        except Exception:
            continue
        if state == "needs_fix":
            stale.append(u)
    drops.scanning("checking meshes", len(utocs), len(utocs))
    if not stale:
        return
    n = len(stale)
    print()
    print(f"  !! {n} pak{'s' if n != 1 else ''} here still "
          f"carr{'y' if n != 1 else 'ies'} PRE-V1.005 meshes. Converted "
          "as-is, the outfit")
    print("     crashes the game the moment it loads.")
    if assume_yes:
        print("     --yes given: converting as-is. To fix, run")
        print(f'       python patch.py --path "{source_hint}" --all')
        print("     and convert again -- or patch the converted output.")
        return
    # NOT _INTERACTED: the conversion still runs after this answer, and its
    # output is the part worth reading. Marking the run "already answered"
    # here skipped the closing pause, so a dropped 1.004 mod converted
    # correctly and then vanished with the window (field report).
    try:
        ans = input("     Patch them now, then convert? (backups are "
                    "kept)  [Y/n] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans in ("", "y", "yes"):
        backups = os.path.join(source_hint, "_patch_backups")
        for u in stale:
            name = os.path.splitext(os.path.basename(u))[0]
            patcher.patch_mod(name, u, backup_dir=backups)
        print("     patched -- converting the fixed files")
    else:
        print("     converting as-is -- patch before playing, or the game "
              "will crash")


def recolour_layout(source, mods):
    """
    (layout, costumes, problem) for a mod that only repaints a costume the
    game already has -- paks of textures and materials under ONE character's
    costume folders, with no model of their own. None when this is not one.

    `layout` is loose_layout's shape: an outfit per pak, all of them variants,
    since colours of one costume belong in one mod. `costumes` is the stock
    folders the recolour was made for: those showing the most of it, almost
    always exactly one. A costume merely sharing a file or two with it would
    show only part of the recolour, so it is not offered. `problem` says why
    it cannot be built instead.
    """
    if not mods or any(up for _u, up in mods):
        return None
    root_path = slots.ROOTS[slots.COSTUME].lower()
    touched, char, folder = set(), None, None
    for utoc, _up in mods:
        try:
            toc = iostore.Toc(utoc)
            try:
                pkgs = rename.read_packages(toc)
            finally:
                toc.close()
        except Exception:
            return None
        if not pkgs or mkdc.find_stock_mesh(pkgs)[0]:
            return None
        for p in pkgs.values():
            bits = p["name"].split("/")
            who = (slots.character_of(slots.COSTUME, bits[4])
                   if len(bits) > 5 and p["name"].lower().startswith(root_path)
                   else None)
            if not who or char not in (None, who):
                return None
            char, folder = who, bits[4]
        touched |= set(pkgs)

    if not slots.have_game():
        return None, [], (
            "these paks only recolour a costume the game already has. "
            "Building them borrows that costume from the game, and the "
            "game's files were not found here.")
    stock = slots.stock(slots.COSTUME)
    order = slots.choices_for(slots.COSTUME, folder)
    meshes = {f"{slots.ROOTS[slots.COSTUME]}{f}/Model/{stock[f]}": f
              for f in order}
    used = stockgraft.samplers(touched, meshes)
    ranked = sorted(used.items(),
                    key=lambda mn: (-mn[1], order.index(meshes[mn[0]])))
    costumes = [meshes[m] for m, n in ranked if n == ranked[0][1]]
    if not costumes:
        if not stockgraft.readable(meshes):
            return None, [], (
                "the game's own costume files are here but could not be "
                "read, so there is no telling which costume these colours "
                "are for.")
        return None, [], (
            "these paks recolour something none of the game's costumes "
            "wears -- there is nothing to put them on.")

    root = os.path.normcase(os.path.abspath(source))
    by_folder = {}
    for u, _up in mods:
        d = os.path.normcase(os.path.dirname(os.path.abspath(u)))
        by_folder.setdefault(d, []).append(u)
    parts = []
    for u, _up in mods:
        d = os.path.dirname(os.path.abspath(u))
        if len(by_folder[os.path.normcase(d)]) > 1:
            rel = template.stem_of(u)
        elif os.path.normcase(d) == root:
            rel = "."
        else:
            rel = os.path.relpath(d, source).replace("\\", "/")
        parts.append((rel, u))
    parts.sort()
    layout = (template.mod_root_name(source), parts, [], [], {r for r, _u in parts})
    return layout, costumes, None


def pick_costume(costumes, assume_yes):
    """
    Which stock costume a recolour goes on, chosen from a list rather than
    typed -- only asked when the files fit more than one equally. The first
    is the answer under --yes and for a bare Enter. None when the person
    backs out.
    """
    global _INTERACTED
    if len(costumes) == 1 or assume_yes:
        return costumes[0]
    print()
    print(f"  These paks recolour one of "
          f"{slots.character_name(costumes[0])}'s costumes. Which one?")
    for i, folder in enumerate(costumes, 1):
        print(f"    {i:2}  {slots.label(folder)}")
    print()
    while True:
        try:
            answer = input("  Pick a number, or Enter for 1: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _INTERACTED = True
            return None
        _INTERACTED = True
        if not answer:
            return costumes[0]
        if answer.isdigit() and 1 <= int(answer) <= len(costumes):
            return costumes[int(answer) - 1]
        print(f"    Not a choice. Give a number from 1 to {len(costumes)}.")


def recolour_outfit(utoc, costume, out_dir, base):
    """
    An outfit container for a recolour pak: the game's own model of
    `costume` beside the pak's textures. From there it converts like any
    costume -- stockgraft carries in the stock materials between the two,
    which is what makes the new textures show.
    """
    name = (f"{slots.ROOTS[slots.COSTUME]}{costume}/Model/"
            f"{slots.stock(slots.COSTUME)[costume]}")
    mesh = stockgraft.stock_package(name)
    if mesh is None:
        raise RuntimeError(f"the game's model for {slots.costume_name(costume)} "
                           "could not be read")
    rec = dict(mesh, bulks=[loosepak.fresh(cid, data)
                            for cid, data in mesh["bulks"]])
    return merge_loose([utoc], out_dir, base,
                       extra={cityhash.package_id(name): rec})


def layout_problem(source, mods):
    """Why loose_layout said no -- in words a person can act on. None when
    this drop is not the loose flow's problem (a Dresscode mod, say)."""
    if not mods or any(up for _u, up in mods):
        return None
    outfits = [u for u, _ in mods if template.carries_outfit(u)]
    if not outfits:
        # A folder of weapon paks alone is a mod in its own right and never
        # reaches here; mixed with anything else, there is no telling which
        # half is the real mod.
        if any(template.is_weapon_pak(u) for u, _ in mods):
            return ("this folder has weapon paks mixed with something that "
                    "is neither weapon nor costume. Drop the weapon paks on "
                    "their own, or add the costume pak they go with.")
        return ("none of these paks carries a costume -- the mod's MAIN "
                "pak is missing from the folder. Drop the whole mod: the "
                "main pak plus its option paks. (Option paks on their own "
                "have nothing to attach to.)")
    root = os.path.normcase(os.path.abspath(source))
    loose_mains = [u for u in outfits if os.path.normcase(
        os.path.dirname(os.path.abspath(u))) == root]
    if len(outfits) > 1 and loose_mains:
        return ("several outfit paks sit at the top of the dropped folder "
                "-- put them all in one subfolder (\"Main\") or give each "
                "its own, then drop again.")
    return ("this folder's shape is not one the converter knows -- see "
            "the README's \"how to organize the folder\" section.")


def check_part_names(extras):
    """
    Entries name an add-on pak by its stem, so two DIFFERENT paks sharing
    one would silently resolve to whichever was read last. Identical copies
    are already down to one by here (see dedupe_paks), so anything left
    over is a real clash. Checked before the template is written, not just
    when it is read back, so the ambiguous list is never handed over.
    """
    seen, clash = set(), set()
    for u in extras:
        stem = os.path.splitext(os.path.basename(u))[0].lower()
        (clash if stem in seen else seen).add(stem)
    if clash:
        raise RuntimeError(
            "more than one add-on pak is called "
            + ", ".join(sorted(clash))
            + ". Rename them apart (or delete the copies you do not want) "
              "and drop the folder again.")


def match_outfit(want, outfits):
    """The outfit `want` names -- by its menu name, its folder, or the last
    part of that folder, whichever the person had in front of them. Returns
    the folder, which is what identifies an outfit; None if no such costume.
    """
    low = str(want).strip().lower()
    for o in outfits:
        if low in (str(o["name"]).lower(), str(o["folder"]).lower(),
                   str(o["folder"]).split("/")[-1].lower()):
            return o["folder"]
    return None


def resolve_variants(meta, extras, outfits=()):
    """
    [(row name, [utoc, ...], only_on, description)] from the template's
    "variants" section -- or one row per extra when the section is absent.

    `only_on` is None when an entry goes on every outfit, else the folders
    of the ones its "outfit" field names -- a mod whose add-on suits only
    some of its outfits says so there.

    Also says whether the person edited the section away from the generated
    default, which must override an exact-restore record: an edit means
    they WANT the change.
    """
    def stem(u):
        return os.path.splitext(os.path.basename(u))[0]

    check_part_names(extras)
    by_stem = {stem(u).lower(): u for u in extras}
    cfg = meta.get("variants")
    default = [(toggles.label_of(u), [u], None, "") for u in extras]
    if cfg is None:
        return default, False
    if not isinstance(cfg, list):
        raise RuntimeError(f'{template.TEMPLATE}: "variants" must be a list')
    out = []
    for k, entry in enumerate(cfg):
        if not isinstance(entry, dict):
            raise RuntimeError(f'{template.TEMPLATE}: variant {k + 1} must be an '
                               'object with "name" and "parts"')
        utocs = []
        for s in entry.get("parts") or []:
            key = os.path.splitext(str(s))[0].lower()
            u = by_stem.get(key)
            if not u:
                raise RuntimeError(
                    f'{template.TEMPLATE}: variant {k + 1} names a pak that is not '
                    f'there: "{s}". The paks are: '
                    + ", ".join(sorted(by_stem)) + ".")
            utocs.append(u)
        if not utocs:
            continue                    # an empty entry is just skipped
        want = entry.get("outfit")
        only_on = None
        # "outfit" aims a costume add-on at particular costumes. A weapon
        # mod has none -- its tiles hang off the character, not a costume --
        # so the field is ignored there rather than refused.
        if not outfits:
            want = None
        if want not in (None, "", []):
            only_on = []
            for w in (want if isinstance(want, list) else [want]):
                folder = match_outfit(w, outfits)
                if folder is None:
                    raise RuntimeError(
                        f'{template.TEMPLATE}: variant {k + 1} is set to go on an '
                        f'outfit that is not there: "{w}". The outfits '
                        "are: " + ", ".join(str(o["name"]) for o in outfits)
                        + ".")
                only_on.append(folder)
        name = str(entry.get("name") or "").strip() \
            or " + ".join(toggles.label_of(u) for u in utocs)
        out.append((name, utocs, only_on,
                    str(entry.get("description") or "").strip()))
    # Structural comparison only, as SETS -- display names differ by which
    # conversion wrote the template, and the two writers list the same
    # entries in different orders; a restore reproduces the original's
    # names and order regardless. Only recomposition means they want a
    # different mod: entries added, removed, made of different parts, or
    # aimed at different costumes.
    def shape(entry):
        _name, us, only_on, _desc = entry
        return (tuple(sorted(u.lower() for u in us)),
                tuple(sorted(only_on)) if only_on else ())

    edited = {shape(e) for e in out} != {shape(e) for e in default}
    return out, edited


def weapon_tiles_back(toc, packages):
    """
    ([(label, renames, objects, keep, preview)], elsewhere) turning the
    weapons-menu tiles THIS TOOL built back into ordinary override paks.

    `elsewhere` names the rows laid out some other way -- an author's own
    weapon mod keeps its files under its plugin's own folders. Those are
    not this function's to place; plan_variants converts them like any
    other row, relocating them under the weapon they stand in for.

    A tile lives under /<Plugin>/Weapons/<Safe>/ with the stock weapon's
    own path kept as the tail, so mapping the tail onto
    /Game/Character/Weapon/ puts every material and texture back over the
    package it was overriding -- which is all a recolour pak ever was.

    The tile's MESH is the stock weapon carried in so Dresscode has
    something to show. A pak needs it only when the mod actually replaced
    it, so it rides along only when its bytes differ from the game's.
    """
    lists = moddata.weapon_lists(toc, weapons.MOD_TYPE_WEAPON)
    if not lists:
        return [], []
    by_low = {p["name"].lower(): p for p in packages.values()}
    out, unmapped = [], []
    for i in lists:
        for row in moddata.read_outfits(toc.read(i)):
            label = str(row.get("name") or "").strip()
            root = str(row.get("skeletal_mesh") or "").split(".")[0]
            mesh = by_low.get(root.lower())
            if not mesh:
                unmapped.append(label or root)
                continue
            under = {p["name"]: p["name"][len(root) + 1:]
                     for p in packages.values()
                     if p["name"].lower().startswith(root.lower() + "/")}
            # Every tail starts with the weapon's own folder, which names
            # the stock mesh: WE0002_15_Tifa_Foo -> .../Model/WE0002_15.
            # A tile that replaced the model outright has no tails at all,
            # and records the weapon in its own name instead.
            folder = (sorted(under.values())[0].split("/")[0] if under
                      else weapons.folder_of_tile(root))
            if not folder:
                unmapped.append(label or root)
                continue
            bits = folder.split("_")
            if len(bits) < 2:
                unmapped.append(label or folder)
                continue
            obj = f"{bits[0]}_{bits[1]}"
            stock_mesh = f"{weapons.WEAPON_ROOT_PROPER}{folder}/Model/{obj}"
            renames = {n.lower(): weapons.WEAPON_ROOT_PROPER + tail
                       for n, tail in under.items()}
            keep = set(under)
            objects = {}
            if weapons.replaces_stock_mesh(toc.read(mesh["chunk"]),
                                           stock_mesh):
                renames[mesh["name"].lower()] = stock_mesh
                keep.add(mesh["name"])
                # Its export was given a digit-free name for the menu; the
                # stock package is referenced by the original one.
                tile_obj = str(row.get("skeletal_mesh") or "").split(".")[-1]
                objects[mesh["name"].lower()] = {tile_obj: obj}
            if not keep:
                unmapped.append(label or folder)
                continue
            out.append((label or folder, renames, objects, keep,
                        row.get("preview_image")))
    return out, unmapped


def note_mixed_shapes(extras, variant_folders):
    """
    A mod may use BOTH shapes: whole costumes in Variants, add-on paks in
    Optional. Every add-on is then offered on every costume, so the menu
    gets a row per costume plus one per costume-and-add-on.
    """
    if not (variant_folders and extras):
        return
    print()
    print(f"      {len(variant_folders)} costumes in Variants\\ and "
          f"{len(extras)} add-on pak{'s' if len(extras) != 1 else ''} in "
          "Optional\\:")
    print("      each add-on is offered on each costume.")


def recorded_parts(source, parts, extras):
    """
    A Dresscode WEAPON mod converts to a pak that overrides a stock weapon
    and carries no costume, so the layout reads it as a weapons-menu add-on
    -- and the exact restore, which rebuilds from THAT pak's bytes, then had
    nothing to rebuild from. Its own record names the folder it came from,
    so promote the pak sitting there back to an outfit part.
    """
    if parts:
        return parts, extras
    try:
        with open(os.path.join(source, template.TEMPLATE), encoding="utf-8") as f:
            recorded = set(
                (unpack_restore(json.load(f).get("restore")) or {})
                .get("variants") or ())
    except (OSError, ValueError):
        return parts, extras
    if not recorded:
        return parts, extras
    root = os.path.normcase(os.path.abspath(source))
    promoted, rest = [], []
    for utoc in extras:
        d = os.path.dirname(os.path.abspath(utoc))
        rel = ("." if os.path.normcase(d) == root
               else os.path.relpath(d, source).replace("\\", "/"))
        (promoted if rel in recorded else rest).append((rel, utoc))
    if not promoted:
        return parts, extras
    return promoted, [u for _rel, u in rest]


def loose_to_dresscode(source, mods, assume_yes=False):
    """
    The template flow for a dropped pak mod. Returns an exit code, or
    None when `source` is not a pak mod root this direction understands.
    """
    layout = template.loose_layout(source, mods)
    costumes = None
    if layout is None:
        got = recolour_layout(source, mods)
        if got:
            layout, costumes, problem = got
            if problem:
                print()
                print(f"  {os.path.basename(source)}: {problem}")
                return 1
    if layout is None:
        problem = layout_problem(source, mods)
        if problem:
            print()
            print(f"  {os.path.basename(source)}: {problem}")
            return 1
        return None
    mod_name, parts, extras, companions, variant_folders = layout
    parts, extras = recorded_parts(source, parts, extras)
    comp_map = {}
    if companions:
        # Folder names said "companion"; the packages have the last word.
        comp_map, options = classify_companions(
            [u for _rel, u in parts], companions)
        companions = sorted({u for us in comp_map.values() for u in us})
        extras = sorted(extras + options)
    check_part_names(extras)

    if not os.path.exists(os.path.join(source, template.TEMPLATE)):
        note_mixed_shapes(extras, variant_folders)
        # A weapon pak needs no combining decision -- its tile stands alone
        # in the weapons menu -- so those entries are written ready-made.
        # The set of weapons a pak covers, not just whether it is one: a
        # single pak routinely does a character's whole set, one tile each.
        covers = {u: template.weapon_tiles_in(u) for u in extras}
        guns = [u for u in extras if covers[u]]
        # A weapon entry is written ready to build, so it has to be written
        # WHOLE: a pak whose textures another pak's materials sample belongs
        # in the same entry or the weapon comes out untextured.
        grouped = dict(template.weapon_entries(guns)) if guns else {}
        flagged = []
        for u in extras:
            if covers[u] and u not in grouped:
                continue                # carried by the entry that needs it
            flagged.append((toggles.label_of(u),
                            template.stem_of(u), covers[u],
                            [template.stem_of(p) for p in grouped.get(u, [u])]))
        costume = None
        if costumes:
            costume = pick_costume(costumes, assume_yes)
            if not costume:
                print("  Nothing converted.")
                return 0
        path = template.write_template(source, mod_name, parts, extras=flagged,
                              costume=costume)
        n_combo = sum(1 for _l, _s, w, _p in flagged if not w)
        n_weap = len(flagged) - n_combo
        n_tiles = sum(len(template.menu_tiles_in(grouped.get(u, [u])))
                      for u in grouped)
        print()
        print(f"  {mod_name}  (pak -> Dresscode)")
        made = (f"{len(parts)} outfit{'s' if len(parts) > 1 else ''}"
                if parts else
                f"{n_tiles} weapon{'s' if n_tiles != 1 else ''}, no costume")
        if costume:
            print(f"      recolour of {slots.costume_name(costume)}")
            made = f"{len(parts)} colour{'s' if len(parts) > 1 else ''}"
        print(f"      created  {os.path.basename(path)}  ({made})")
        if n_weap and parts:
            print()
            print(f"      {n_weap} weapon pak{'s' if n_weap != 1 else ''} "
                  f"set up as WEAPONS-menu tile{'s' if n_weap != 1 else ''}"
                  " -- nothing to do.")
        elif n_weap:
            print()
            print(f"      The menu is already written: {n_tiles} "
                  f"WEAPONS-menu tile{'s' if n_tiles != 1 else ''}.")
            print("      Rename them if you like -- the stock weapon tile "
                  "switches them off.")
        if n_combo:
            print()
            print(f"      This mod has {n_combo} add-on pak"
                  f"{'s' if n_combo != 1 else ''}, not in the menu yet.")
            print(f"      Open {template.TEMPLATE} and list the combinations you "
                  "wear (one entry")
            print("      = one tile), or set \"stackable\": true to keep "
                  "the parts as")
            print("      drop-in files. The file explains both.")
        if flagged:
            print()
        print("      NOTHING IS CONVERTED YET -- this first drop only "
              "writes")
        print(f"      {template.TEMPLATE}. Drop the same folder on convert.py again")
        print("      to build the mod. (Names, author and pictures are")
        print(f"      optional -- {template.TEMPLATE} explains.)")
        return 0

    meta, outfits = template.read_template(source, parts)
    costume = None
    if costumes:
        costume = meta["costume"]
        # Missing, or edited to a costume these colours were not made for.
        if costume not in costumes:
            costume = pick_costume(costumes, assume_yes)
            if not costume:
                print("  Nothing converted.")
                return 0
    variants, variants_edited = resolve_variants(meta, extras, outfits)
    rt = unpack_restore(meta.get("restore"))
    # A record with nothing to rebuild FROM cannot restore anything: a mod
    # whose every row was a weapon tile this tool built hands those back as
    # override paks, which the fresh build turns into the same tiles again.
    restorable = bool(rt and (rt.get("variants") or rt.get("optionals")))
    if restorable and needs_sidecar(rt) \
            and not os.path.exists(os.path.join(source, SIDECAR)):
        print(f"      {SIDECAR} is not here -- building fresh instead of "
              "restoring the original")
        restorable = False
    exact = restorable and restore_matches(rt, meta, outfits, extras) \
        and not variants_edited
    plugin = rt["plugin"] if exact else template.plugin_id(meta["name"])
    # Several outfits, each with its own toggles, in one mod make a menu
    # where nothing says which toggle belongs to which outfit -- so each
    # outfit becomes a Dresscode mod of its own. A restore is exempt: it
    # reproduces the original mod, whatever shape that was.
    #
    # Variants are declared to belong together, so they stay one mod with a
    # tile each -- and add-on paks alongside them are offered on every
    # variant, which is the one case where naming the toggle is unambiguous.
    split = not exact and len(outfits) > 1 and not variant_folders
    # A weapon mod's rows ARE its variants -- nothing about it is per-outfit,
    # so the lines below count weapons instead. One entry is not one tile:
    # a pak covering a character's whole set becomes a tile per weapon.
    gun_tiles = ([len(template.menu_tiles_in(us))
                  for _n, us, _o, _d in variants]
                 if not outfits else [])
    guns = sum(gun_tiles)
    print()
    print(f"  {meta['name']}  (pak -> Dresscode"
          + (f", {len(outfits)} {'colours' if costume else 'outfits'}"
             if len(outfits) > 1 else "")
          + (f", {guns} weapon{'s' if guns != 1 else ''}" if guns else "")
          + ")")
    if costume:
        print(f"      recolour of {slots.costume_name(costume)}")
    if exact:
        print("      this folder came from a Dresscode mod -- restoring "
              "the original exactly")
    elif rt is not None:
        print("      edited since it was converted -- building fresh from "
              "the changed values")
    if meta["author"]:
        print(f"      by {meta['author']}")
    icon = (os.path.basename(meta["icon"]) if meta["icon"]
            else "none (optional -- a picture next to dresscode.json)")
    print(f"      thumbnail: {icon}")
    if split:
        print(f"      -> {os.path.join(os.path.dirname(source), f'{plugin} (Dresscode)')}{os.sep}"
              f"   ({len(outfits)} separate mods, one per outfit)")
    else:
        print(f"      -> {os.path.join(os.path.dirname(source), f'{plugin} (Dresscode)', plugin)}{os.sep}"
              f"   (goes into End\\Mods)")
    for k, o in enumerate(outfits):
        pic = (os.path.basename(o["preview"]) if o["preview"]
               else "no picture (optional)")
        print(f"      {k + 1}. {o['name']}   [{pic}]")
    for k, (name, us, _only, _d) in enumerate(variants if guns else ()):
        n_t = gun_tiles[k]
        pic = next((os.path.basename(p) for u in reversed(us)
                    for p in [template.pak_image(u)] if p), None)
        print(f"      {k + 1}. {name}   "
              + (f"[{n_t} weapons" if n_t != 1 else "[weapon")
              + (f", {pic}]" if pic else "]"))
    # Aiming a weapon entry at a costume reads as if it would work.
    aimed = [n for n, us, only, _d in variants
             if only and all(template.weapon_tiles_in(u) for u in us)]
    if aimed:
        print(f'      note: "outfit" does nothing on a weapon entry '
              f'({aimed[0]}) -- weapons are not tied to costumes')
    # Stackable is about keeping add-ons as drop-in files ALONGSIDE a costume
    # in the menu. With no costume there is nothing left in the menu to keep.
    stackable = (meta["stackable"] and bool(extras) and not exact
                 and bool(outfits))
    if companions and not exact:
        n = len(companions)
        print(f"      + {n} required companion pak{'s' if n != 1 else ''} "
              + ("merged into the outfits they ship with"
                 if len(outfits) > 1 else "merged into the outfit"))
    if extras and not exact and not guns:      # weapons are listed above
        if stackable:
            print(f"      + {len(extras)} extra paks stay COMBINABLE: the "
                  "shared masks ride in ~mods")
            print("        and the original Optional paks keep working on "
                  "top of the Dresscode outfit")
        elif not variants:
            # Said out loud, because "the parts are in the folder" and "the
            # parts are in the mod" look identical once the build succeeds.
            print(f"      + the {len(extras)} add-on pak"
                  f"{'s' if len(extras) != 1 else ''} are NOT in this mod --")
            print(f"        you have not said which outfits to make. Open "
                  f"{template.TEMPLATE}:")
            print("        list the combinations you wear, or set")
            print("        \"stackable\": true to keep them as drop-in files "
                  "for ~mods.")
        else:
            combos = sum(1 for _n, us, _cs, _d in variants if len(us) > 1)
            print(f"      + {len(variants)} tile"
                  f"{'s' if len(variants) != 1 else ''} to build from "
                  f"{len(extras)} add-on pak{'s' if len(extras) != 1 else ''}"
                  + (f" ({combos} combining several)" if combos else ""))
    # Even an exact restore rebuilds from these bytes: a 1.004-era mod
    # restored is a 1.004-era mod, and it crashes just the same.
    preflight_meshes([u for _rel, u in parts] + list(extras)
                     + list(companions), assume_yes, source)
    print()
    if not confirm(assume_yes, max(len(outfits), guns)):
        print("  Nothing converted.")
        return 0

    # A wrapper folder, so the output can never land on (and overwrite) an
    # existing copy of the mod -- roundtrips make that collision routine. The
    # folder INSIDE keeps the exact plugin name Dresscode requires.
    out_root = os.path.join(os.path.dirname(source), f"{plugin} (Dresscode)")
    merge_tmp = None
    if exact:
        # Extras are found by their pak's name, wherever the folder ended up.
        by_pak = {os.path.splitext(os.path.basename(u))[0].lower(): u
                  for u, _up in mods}
        opt = {rel: by_pak.get(str(v.get("pak", "")).lower())
               for rel, v in (rt.get("optionals") or {}).items()}
        parts_by_folder = {o["folder"]: o["utoc"] for o in outfits}
        bin_path = os.path.join(source, SIDECAR)
        roots = [mkdc.restore(rt, parts_by_folder, out_root, optionals=opt,
                              sidecar=bin_path)]
        # Library mods inlined on the way out come back as themselves --
        # every mod exactly as downloaded, from the same paks.
        for lib in rt.get("libraries") or []:
            roots.append(mkdc.restore(lib, parts_by_folder, out_root,
                                      optionals=opt, sidecar=bin_path))
    else:
        if costume:
            # A recolour carries no model; from here on the game's own,
            # beside the pak's textures, IS the outfit.
            merge_tmp = os.path.join(out_root, "_merge_tmp")
            for k, o in enumerate(outfits):
                o["utoc"] = recolour_outfit(o["utoc"], costume, merge_tmp,
                                            f"Recolour{k + 1}_P")
                o["recolour"] = True
        if companions:
            # The outfit cannot render without them, so from here on the
            # merged container IS the outfit.
            merge_tmp = os.path.join(out_root, "_merge_tmp")
            for k, o in enumerate(outfits):
                mine = comp_map.get(o["utoc"]) or []
                if mine:
                    o["utoc"] = merge_loose([o["utoc"]] + mine, merge_tmp,
                                            f"Merged{k + 1}_P")
        ex = [] if stackable else variants
        ext = stack_tree(outfits, extras) if stackable else ()
        pics = {}
        for u in extras:
            pic = template.pak_image(u)
            if pic:
                pics[os.path.normcase(os.path.abspath(u))] = pic
        used, roots = set(), []
        # One plugin per outfit means every note repeats per plugin, and the
        # notes are about the paks, not the outfit. Say each once.
        said = set()

        def once(msg):
            if msg not in said:
                said.add(msg)
                print(msg)

        for o in (outfits if split else [None]):
            if split:
                sub_name = f"{meta['name']} - {o['name']}"
                sub = template.safe_plugin_id(sub_name, used)
                roots.append(mkdc.build(dict(meta, name=sub_name), [o],
                                        sub, out_root, extras=ex,
                                        external=ext, say=once,
                                        weapon_tiles=o is outfits[0],
                                        part_previews=pics))
            else:
                roots.append(mkdc.build(meta, outfits, plugin, out_root,
                                        extras=ex, external=ext,
                                        part_previews=pics))
    mods_dir = None
    if stackable:
        # One masks pak serves every outfit -- they share the tree. The
        # extras ride along untouched, so the folder is a complete kit.
        mods_dir = os.path.join(out_root, "Put in ~mods")
        masks_base = f"Z8_{plugin}_MASKS_P"
        written = write_masks_pak(outfits[0]["utoc"], ext, mods_dir,
                                  masks_base)
        problems = rename.verify(written)
        if problems:
            print()
            print("  PROBLEM -- the masks pak is not sound, do not "
                  "install it:")
            for p in problems[:8]:
                print(f"    {p}")
            return 1
        print(f"    written  {masks_base}  "
              f"({len(ext)} shared files, verified)")
        for utoc in extras:
            stem = os.path.splitext(utoc)[0]
            for suffix in (".utoc", ".ucas", ".pak"):
                if os.path.exists(stem + suffix):
                    shutil.copy2(stem + suffix, mods_dir)
        print(f"    copied   {len(extras)} original Optional paks beside it")
    if merge_tmp:
        shutil.rmtree(merge_tmp, ignore_errors=True)
    for root in roots:
        name = os.path.basename(root)
        written = os.path.join(root, "Content", "Paks", "WindowsNoEditor",
                               f"{name}End-WindowsNoEditor.utoc")
        live = sys.stdout.isatty()
        if live:
            print(f"    checking {os.path.basename(written)} ...",
                  end="", flush=True)
        problems = rename.verify(written)
        if live:
            print("\r" + " " * 70 + "\r", end="", flush=True)
        if problems:
            print()
            print("  PROBLEM -- the converted mod is not sound, "
                  "do not install it:")
            for p in problems[:8]:
                print(f"    {p}")
            return 1
        print(f"    checked  {os.path.basename(written)} is internally "
              "consistent")
    print()
    if len(roots) > 1:
        print(f"  Done. Copy the {len(roots)} folders from inside "
              f"\"{os.path.basename(out_root)}\" into the game's End\\Mods")
        print("  folder (where Dresscode itself is installed):")
        for root in roots:
            print(f"    {os.path.basename(root)}")
    else:
        print(f"  Done. Copy the \"{os.path.basename(roots[0])}\" folder "
              f"from inside \"{os.path.basename(out_root)}\" into the")
        print("  game's End\\Mods folder (where Dresscode itself is "
              "installed).")
    if mods_dir:
        print(f"  Then copy the FILES from \"{os.path.basename(mods_dir)}\" "
              "into Content\\Paks\\~mods --")
        print("  the masks pak always, plus whichever Optional paks you "
              "want active. They combine freely.")
    return 0


def folder_name(text):
    """`text` reduced to something Windows accepts as a folder name."""
    cleaned = "".join(" " if c in '<>:"/\\|?*' or ord(c) < 32 else c
                      for c in text)
    return " ".join(cleaned.split()).strip(" .")


def prepare_to_loose(toc, uplugin, out_base=None,
                     keep_registration=False):
    """
    Plan a mod's conversions and print their summaries. Returns a list of
    zero-argument callables, one per variant -- planning is separated from
    writing so a multi-mod drop can show everything before one confirmation.

    A single-outfit mod writes straight into "<Mod> (pak)". Variants
    each get a sub-folder inside it, named for the outfit, and every variant
    keeps the same file name -- they all replace the same stock costume, so
    installing one over another in ~mods swaps them cleanly.

    `out_base` overrides where the output folder goes: beside the mod's own
    folder normally, but a mod unpacked from an archive lives in a temp
    folder, so its output belongs beside the archive instead.
    """
    plugin = os.path.splitext(os.path.basename(uplugin))[0]

    # Undeclared library dependencies (a shared skin mod): inline their
    # packages by planning over a MERGED container, so cross-plugin imports
    # become internal and every rewrite fixes them like any other.
    libraries, merged_toc, merge_tmp = [], None, None
    lib_roots = analyse.foreign_roots(toc, plugin)
    found = {}
    if lib_roots:
        found, missing = analyse.locate_libraries(lib_roots, uplugin)
        # A missing library is how the mod ITSELF behaves when the other
        # mod is not installed -- convert what is here, say what is not.
        for root in missing:
            print(f"  note: {plugin} references {root}, which is not here "
                  "-- converting without it, as the game would run it")
        # Repeated at the END too (see below). At the top of a long
        # conversion this line scrolls away, and what the player sees for it
        # is a grey checkerboard costume with nothing to explain why.
    if found:
        print(f"  {plugin} needs "
              f"{'these mods' if len(found) > 1 else 'another mod'}, and "
              f"{'they ride' if len(found) > 1 else 'it rides'} along:")
        for root, (_utoc, _up, where) in found.items():
            print(f"      + {root}   ({where})")
        merge_tmp = tempfile.mkdtemp(prefix="dcdep-")
        merged_utoc = merge_loose(
            [toc.path] + [utoc for utoc, _up, _w in found.values()],
            merge_tmp, f"{plugin}Merged_P")
        merged_toc = iostore.Toc(merged_utoc)
        libraries = [(root, utoc, up)
                     for root, (utoc, up, _w) in found.items()]
        orig_toc, toc = toc, merged_toc
    else:
        orig_toc = toc

    plans, toggles, ctx = analyse.plan_variants(
        toc, plugin, extra_roots=tuple(r for r, _u, _p in libraries),
        keep_registration=keep_registration)

    source_root = os.path.abspath(os.path.dirname(uplugin)).rstrip("\\/")
    mod_out = (os.path.join(out_base, os.path.basename(source_root) + " (pak)")
               if out_base else source_root + " (pak)")
    base = f"{plugin}_P"

    # One scannable block per mod: what it is, where it goes, and the variant
    # list -- per-variant paths and package details would drown a multi-mod
    # drop. Characters are named per variant only when they differ.
    n = len(plans)
    chars = [o["player_type"].split("::")[-1].title() for o, *_ in plans]
    mixed = len(set(chars)) > 1
    guns = [o.get("weapon") for o, *_ in plans]
    # Tiles this tool built are handed back further down rather than planned,
    # so a mod made only of them plans nothing and still has content.
    weapon_backs, _elsewhere = weapon_tiles_back(toc, ctx["packages"])
    own_tiles = len(weapon_backs) if not n else 0
    head = f"  {plugin}  (Dresscode"
    if n > 1:
        head += f", {n} " + ("weapons" if all(guns) else "outfits")
    elif own_tiles:
        head += f", {own_tiles} weapon{'s' if own_tiles != 1 else ''}"
    if chars and not mixed:
        what = ("weapon" if all(guns) else
                "outfit and weapon" if any(guns) else "standard outfit")
        head += f", replaces {chars[0]}'s {what}"
    print()
    print(head + ")")
    print(f"      -> {mod_out}{os.sep}")

    runners, used, layout = [], set(), []
    for k, (outfit, target, renames, objects, drop) in enumerate(plans):
        if n == 1:
            out_dir, label = mod_out, os.path.basename(mod_out)
        else:
            # Named for the outfit; authors reuse display names across
            # variants, so a clash falls back to the mesh's own name -- and
            # when even the meshes share a name (four rows all called the
            # same, every mesh "PC0003_00"), a counter. Without it, variants
            # silently overwrote each other's folders.
            sub = folder_name(outfit["name"])
            if not sub or sub.lower() in used:
                mesh_leaf = outfit["skeletal_mesh"].split(".")[-1]
                sub = folder_name(f"{outfit['name']} ({mesh_leaf})".strip())
            stem, dup = sub, 1
            while sub.lower() in used:
                dup += 1
                sub = f"{stem} {dup}"
            used.add(sub.lower())
            # Under Variants\, which is what the folder means on the way
            # back: several costumes that belong to ONE mod. Plain
            # subfolders would come back as separate mods, one per outfit,
            # which is not the mod that went in.
            out_dir = os.path.join(mod_out, VARIANTS_DIR_OUT, sub)
            label = f"{VARIANTS_DIR_OUT}/{sub}"
            print(f"      {k + 1}. {sub}"
                  + (f"   ({chars[k]})" if mixed else ""))
        layout.append(("." if n == 1 else label,
                       (outfit, target, renames, objects, drop)))

        lroot = analyse.loose_root_of(target)

        def run(renames=renames, objects=objects, drop=drop,
                out_dir=out_dir, label=label, lroot=lroot):
            written = rename.rename_container(toc, renames,
                                              mount_of_common(lroot),
                                              analyse.loose_path_under(lroot),
                                              out_dir, base,
                                              container_name=base,
                                              object_renames=objects,
                                              drop=drop, fix_arcs=True,
                                              quiet=True)
            with open(os.path.join(out_dir, base + ".pak"), "wb") as f:
                f.write(pakfile.build(pakfile.LOOSE_MOUNT))

            problems = rename.verify(written)
            if problems:
                print()
                print(f"  PROBLEM -- {label} is not sound, do not install it:")
                for p in problems[:8]:
                    print(f"    {p}")
                if len(problems) > 8:
                    print(f"    ... and {len(problems) - 8} more")
                return 1
            mb = os.path.getsize(os.path.splitext(written)[0] + ".ucas") \
                / (1024 * 1024)
            print(f"    converted  {label}   ({mb:,.1f} MB, verified)")
            return 0

        runners.append(run)

    # ---- optional paks, the pre-Dresscode modular style ------------------
    packages = ctx["packages"]
    by_low = {p["name"].lower(): pid for pid, p in packages.items()}
    mesh_rel = {}                       # mesh pid -> variant folder label
    for rel, (o, *_r) in layout:
        pid = by_low.get(o["skeletal_mesh"].split(".")[0].lower())
        if pid is not None:
            mesh_rel[pid] = rel
    opt_layout, taken = [], {}
    for k, t in enumerate(toggles):
        # An extra sits next to what it applies to: inside an outfit's own
        # folder when it belongs to that outfit alone -- a mesh-carrying one
        # always does -- and at the top when it fits any of them.
        home = (mesh_rel.get(t["mesh_pid"], ".")
                if len(t["bases"]) == 1 and len(layout) > 1 else ".")
        label = folder_name(t["row"]["name"]) or \
            folder_name(t["bp"].rsplit("/", 1)[-1].replace("_", " ")) or "extra"
        here = taken.setdefault(home, set())
        stem, dup = label, 1
        while label.lower() in here:
            dup += 1
            label = f"{stem} {dup}"
        here.add(label.lower())
        parent = mod_out if home == "." else os.path.join(mod_out, home)
        out_dir = os.path.join(parent, "Optional", label)
        rel = "/".join(([] if home == "." else [home]) + ["Optional", label])
        # A digit prefix sorts before the base pak's name, which is the
        # load order the modular standard relies on for overrides to win.
        opt_base = f"0{chr(65 + (k % 26))}_{template.plugin_id(label)}_P"
        print(f"      + optional: {label}"
              + (f"   (for {home})" if home != "." else "")
              + f"   ({len(t['swaps'])} material "
              f"swap{'s' if len(t['swaps']) != 1 else ''}"
              + (", carries the outfit mesh" if t["kind"] == "mesh" else "")
              + ")")

        if t["kind"] == "mesh":
            # Clean slots swap by PACKAGE OVERRIDE (proven in game); a
            # shared slot repoints at a clean slot's EXISTING material
            # import -- a 4-byte patch in the mesh's material table, which
            # then picks up the same override. Renames are the BASE
            # variant's own plus the overrides, so the mesh overrides the
            # base's and everything else lines up.
            plan = next((p for rel, p in layout
                         if by_low.get(p[0]["skeletal_mesh"].split(".")[0]
                                       .lower()) == t["mesh_pid"]), None)
            if plan is None:
                print(f"          skipped: {label}: no base outfit carries "
                      "its mesh")
                continue
            _o, _target, renames, objects, _drop = plan
            renames = dict(renames)
            objects = {k2: dict(v) for k2, v in objects.items()}
            for base_pkg, repl_pkg in t["overrides"].items():
                renames[repl_pkg.lower()] = renames.get(
                    base_pkg.lower(),
                    analyse.converted_name(base_pkg, ctx["roots"],
                                   t["costume_root"]))
            for k2, v in t["objects"].items():
                objects.setdefault(k2, {}).update(v)

            mesh_low = packages[t["mesh_pid"]]["name"].lower()
            repoint = {}
            for slot, anchor in t["repoint"].items():
                new_pkg = renames.get(anchor[0].lower(), anchor[0])
                repoint[slot] = cityhash.object_id(new_pkg, anchor[1])
            post_edit = {mesh_low:
                         (lambda d, r=repoint: matpack.repoint_slots(d, r))}
            extra_deps = None
        else:
            # EVERY package gets its base-conversion name -- dropped ones
            # too, so the kept materials' texture references and dependency
            # records point where the base pak actually serves them.
            # Override sources then land on the package they replace.
            renames = {p["name"].lower():
                       analyse.converted_name(p["name"], ctx["roots"],
                                      t["costume_root"])
                       for p in packages.values()}
            for base_pkg, repl_pkg in t["overrides"].items():
                renames[repl_pkg.lower()] = analyse.converted_name(
                    base_pkg, ctx["roots"], t["costume_root"])
            # The carried parent takes a side name, which repoints every
            # reference the replacement holds on the package it replaces --
            # left at the base name they would point at the replacement
            # itself.
            for base_pkg in t["carry"]:
                renames[base_pkg.lower()] = analyse.converted_name(
                    base_pkg, ctx["roots"], t["costume_root"]) + PARENT_ASIDE
            objects = dict(t["objects"])
            post_edit = None
            extra_deps = None

        keep_names = {packages[pid]["name"] for pid in t["keep"]}
        drop = {p["name"] for p in packages.values()
                if p["name"] not in keep_names}
        new_names = list(renames[n.lower()] for n in keep_names)
        common = os.path.commonprefix([n + "/" for n in new_names])
        common = common[:common.rfind("/") + 1]
        if not common.startswith("/Game/"):
            print(f"          skipped: {label} changes files a pak cannot carry")
            continue

        def opt_run(renames=renames, objects=objects, drop=drop,
                    out_dir=out_dir, opt_base=opt_base, label=label,
                    common=common, post_edit=post_edit,
                    extra_deps=extra_deps):
            written = rename.rename_container(
                toc, renames, mount_of_common(common), lambda n,
                c=common: n[len(c):], out_dir, opt_base,
                container_name=opt_base, object_renames=objects,
                drop=drop, fix_arcs=True, quiet=True, cross_pak=True,
                post_edit=post_edit, extra_deps=extra_deps)
            with open(os.path.join(out_dir, opt_base + ".pak"), "wb") as f:
                f.write(pakfile.build(pakfile.LOOSE_MOUNT))
            problems = rename.verify(written)
            if problems:
                print(f"  PROBLEM -- optional {label} is not sound:")
                for p in problems[:6]:
                    print(f"    {p}")
                return 1
            mb = os.path.getsize(os.path.splitext(written)[0] + ".ucas") \
                / (1024 * 1024)
            print(f"    optional   {label}   ({mb:,.2f} MB, verified)")
            return 0

        runners.append(opt_run)
        opt_layout.append((rel, opt_base, t))

    # ---- weapons-menu tiles, back to override paks -----------------------
    # Their registration asset is dropped like every other one, so nothing
    # downstream would ever see them: mapped back here or lost entirely.
    gun_layout = []
    for w, (wlabel, wrenames, wobjects, wkeep,
            wpreview) in enumerate(weapon_backs):
        label = folder_name(wlabel) or f"weapon {w + 1}"
        out_dir = os.path.join(mod_out, "Optional", label)
        wdrop = {p["name"] for p in packages.values()
                 if p["name"] not in wkeep}
        wbase = f"0W{chr(65 + (w % 26))}_{template.plugin_id(label)}_P"
        print(f"      + weapon: {label}   ({len(wkeep)} file"
              f"{'s' if len(wkeep) != 1 else ''})")
        gun_layout.append((f"Optional/{label}", wbase, wpreview))

        def weapon_run(renames=wrenames, objects=wobjects, drop=wdrop,
                       out_dir=out_dir, wbase=wbase, label=label):
            written = rename.rename_container(
                toc, renames, mount_of_common(weapons.WEAPON_ROOT_PROPER),
                lambda n: n[len(weapons.WEAPON_ROOT_PROPER):],
                out_dir, wbase, container_name=wbase,
                object_renames=objects, drop=drop, fix_arcs=True,
                quiet=True, cross_pak=True)
            with open(os.path.join(out_dir, wbase + ".pak"), "wb") as f:
                f.write(pakfile.build(pakfile.LOOSE_MOUNT))
            problems = rename.verify(written)
            if problems:
                print(f"  PROBLEM -- weapon {label} is not sound:")
                for p in problems[:6]:
                    print(f"    {p}")
                return 1
            mb = os.path.getsize(os.path.splitext(written)[0] + ".ucas") \
                / (1024 * 1024)
            print(f"    weapon     {label}   ({mb:,.2f} MB, verified)")
            return 0

        runners.append(weapon_run)

    if plans and (opt_layout or gun_layout):
        print("      install: one outfit folder's three files go in ~mods; "
              "extras from its Optional folder go in alongside")
    elif opt_layout or gun_layout:
        # A weapon mod writes nothing BUT Optional folders -- there is no
        # outfit folder to send the reader to first.
        print("      install: the three files from an Optional folder "
              "go in ~mods")
    elif len(plans) > 1:
        print("      install: pick one Variants folder; its three files "
              "go in ~mods")

    def warn_missing():
        """The last thing printed, because it decides whether the result
        works: a mod whose partner is absent converts fine and then renders
        as a grey checkerboard in game."""
        if not lib_roots or not missing:
            return 0
        print()
        print("  !! THIS MOD NEEDS ANOTHER MOD, and it was not here:")
        for root in missing:
            print(f"       {root}")
        print("     Converted without it -- what it provides (skin, usually)")
        print("     shows as grey checkers. To fix, put that mod's folder")
        print("     next to this one and convert again.")
        return 0

    def record():
        code = record_roundtrip(toc, uplugin, plugin, plans, ctx, mod_out,
                                layout, opt_layout, gun_layout,
                                orig=orig_toc, libraries=libraries)
        if merged_toc is not None:
            merged_toc.close()
            shutil.rmtree(merge_tmp, ignore_errors=True)
        return code

    runners.append(record)
    runners.append(warn_missing)
    return runners


def mount_of_common(common):
    """The container mount for a common /Game/ folder prefix."""
    return "../../../End/Content/" + common[len("/Game/"):]


# Set once a prompt has handled the final keypress, so the end-of-run pause
# does not demand a second Enter.
_INTERACTED = False


def confirm(assume_yes, count):
    global _INTERACTED
    if assume_yes:
        return True
    word = "these" if count > 1 else "this"
    try:
        ans = input(f"  Convert {word}?  [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        _INTERACTED = True
        return False
    if ans in ("y", "yes"):
        return True
    _INTERACTED = True
    return False


def gather(source, temps):
    """
    Yield (utoc, uplugin, out_base) for every mod a dropped `source` holds.

    Archives -- dropped directly, or found inside a dropped folder -- are
    unpacked to a temp folder (nested archives included), scanned there, and
    their conversions anchored beside the archive they came from. Mods already
    on disk are anchored beside their own folder.
    """
    if drops.is_archive(source):
        tmp = tempfile.mkdtemp(prefix="convert-")
        temps.append(tmp)
        print(f"  Unpacking {os.path.basename(source)} ...")
        drops.extract_archive(source, tmp)
        drops.expand_archives(tmp)
        for utoc, uplugin in analyse.find_mods(tmp):
            yield utoc, uplugin, os.path.dirname(source)
        return

    for utoc, uplugin in analyse.find_mods(source):
        yield utoc, uplugin, None
    for arc in drops.archives_in(source):
        yield from gather(arc, temps)


def unpack_loose_archive(source, temps, assume_yes):
    """
    A dropped ARCHIVE of a pak mod. The folder flow needs a real folder
    -- dresscode.json lives there, pictures go there, the person edits
    there -- so the archive is unpacked once BESIDE ITSELF and that folder
    is the mod from then on. Dropping the archive again just uses it, so
    zip-drop-twice behaves exactly like folder-drop-twice. Returns the
    flow's exit code, or None when the archive is not a pak mod (a
    Dresscode mod's flow reads archives directly).
    """
    tmp = tempfile.mkdtemp(prefix="convert-")
    temps.append(tmp)
    print(f"  Unpacking {os.path.basename(source)} ...")
    drops.extract_archive(source, tmp)
    drops.expand_archives(tmp)
    mods = analyse.find_mods(tmp)
    if not mods or any(up for _u, up in mods):
        return None
    dest = os.path.splitext(source)[0]
    if os.path.exists(dest):
        print(f"  Using the folder already beside it: "
              f"{os.path.basename(dest)}{os.sep}")
    else:
        inner = tmp
        entries = os.listdir(tmp)
        if len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])):
            inner = os.path.join(tmp, entries[0])
        shutil.move(inner, dest)
        print(f"  Unpacked beside the archive: "
              f"{os.path.basename(dest)}{os.sep}")
        print("  Use that folder from here on -- dresscode.json is "
              "generated inside it.")
    handled = loose_to_dresscode(dest, analyse.find_mods(dest), assume_yes)
    return 1 if handled is None else handled


def unpack_archive_folder(source, temps, assume_yes):
    """
    A dropped FOLDER of archives -- authors ship modular mods as one zip
    per piece. Everything unpacks into ONE new folder beside it, each
    archive into its own subfolder, and that folder becomes the mod --
    the same flow as dropping a single zip. Returns the flow's exit code,
    or None when this is not a pak mod (Dresscode archives already read
    fine from their zips).
    """
    archives = drops.archives_in(source)
    if not archives:
        return None
    tmp = tempfile.mkdtemp(prefix="convert-")
    temps.append(tmp)
    print(f"  Unpacking {len(archives)} archive"
          f"{'s' if len(archives) != 1 else ''} from "
          f"{os.path.basename(source)} ...")
    for n, a in enumerate(archives, 1):
        drops.progress("unpacking", n, len(archives), a)
        sub = os.path.join(tmp, folder_name(
            os.path.splitext(os.path.basename(a))[0]) or "mod")
        os.makedirs(sub, exist_ok=True)
        drops.extract_archive(a, sub)
    drops.progress_done()
    drops.expand_archives(tmp)
    mods = analyse.find_mods(tmp)
    if not mods or any(up for _u, up in mods):
        return None
    dest = source.rstrip("\\/") + " (unpacked)"
    if os.path.exists(dest):
        print(f"  Using the folder already beside it: "
              f"{os.path.basename(dest)}{os.sep}")
    else:
        shutil.move(tmp, dest)
        print(f"  Unpacked beside it: {os.path.basename(dest)}{os.sep}")
        print("  Use that folder from here on -- dresscode.json is "
              "generated inside it.")
    handled = loose_to_dresscode(dest, analyse.find_mods(dest), assume_yes)
    return 1 if handled is None else handled


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    assume_yes = "-y" in argv or "--yes" in argv
    # --keep-registration converts without dropping the two Dresscode
    # registration assets, so the container header is remapped in place
    # instead of rebuilt -- it isolates the drop path when bisecting
    # a broken output.
    keep_registration = "--keep-registration" in argv
    if not args:
        print(__doc__.strip())
        return 2

    runners, code, temps, tocs = [], 0, [], []
    handled_any = False
    try:
        for raw in args:
            source = os.path.abspath(raw.rstrip("\\/"))
            if not os.path.exists(source):
                print(f"  Not found: {source}")
                code = 1
                continue
            if os.path.isdir(source):
                # A bad template in one dropped folder must not abort the rest.
                try:
                    handled = loose_to_dresscode(source, analyse.find_mods(source),
                                                 assume_yes)
                except RuntimeError as ex:
                    print(f"  {ex}")
                    code = max(code, 1)
                    continue
                if handled is not None:
                    handled_any = True
                    code = max(code, handled)
                    continue
                result = unpack_archive_folder(source, temps, assume_yes)
                if result is not None:
                    handled_any = True
                    code = max(code, result)
                    continue
            elif drops.is_archive(source):
                result = unpack_loose_archive(source, temps, assume_yes)
                if result is not None:
                    handled_any = True
                    code = max(code, result)
                    continue
            found = False
            for utoc, uplugin, out_base in gather(source, temps):
                found = True
                if not uplugin:
                    print()
                    print(f"  {os.path.basename(utoc)}  (pak -- drop "
                          "its own folder to convert toward Dresscode)")
                    code = max(code, 1)
                    continue
                try:
                    # Before the container is opened: patching rewrites it.
                    preflight_meshes([utoc], assume_yes,
                                     os.path.dirname(uplugin))
                    toc = iostore.Toc(utoc)
                    tocs.append(toc)
                    runners += prepare_to_loose(
                        toc, uplugin, out_base,
                        keep_registration=keep_registration)
                except RuntimeError as ex:
                    print(f"  {os.path.basename(utoc)}: {ex}")
                    code = max(code, 1)
                except Exception as ex:
                    print(f"  Could not read {os.path.basename(utoc)}: {ex}")
                    code = max(code, 1)
            if not found:
                print(f"  No mod files (.utoc) found in {source}")
                code = max(code, 1)

        if not runners:
            # "Nothing found at all" is a failure; a drop fully handled by
            # the template flow is not.
            return code if handled_any else (code or 1)

        print()
        if not confirm(assume_yes, len(runners)):
            print("  Nothing converted.")
            return code
        for run in runners:
            code = max(code, run())
        print()
        print("  Done. Copy the three files from the folder you want into "
              "the game's")
        print("  End\\Content\\Paks\\~mods folder.")
        return code
    finally:
        for toc in tocs:                # open handles block temp deletion
            toc.close()
        for tmp in temps:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _code = 1
    try:
        _code = main(sys.argv[1:])
    except RuntimeError as ex:
        print(f"  {ex}")
    except Exception:
        # Anything unexpected must still leave a readable window: a
        # drag-and-drop console closes with the process, taking the
        # traceback with it.
        import traceback
        print()
        print("  Unexpected error -- nothing was harmed, but please report "
              "this:")
        print()
        traceback.print_exc(file=sys.stdout)
    drops.pause_before_exit(sys.argv[1:], _INTERACTED)
    sys.exit(_code)
