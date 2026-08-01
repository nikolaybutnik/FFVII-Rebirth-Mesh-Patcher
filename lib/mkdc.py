"""
mkdc.py -- assembles a complete Dresscode plugin from loose pak mods.

The forward conversion's mirror. Takes one loose pak per outfit, renames each
outfit's mesh out of the stock costume slot into the plugin's namespace,
merges everything into one container, synthesizes what a loose pak never had
-- the two registration assets, preview textures, the AssetRegistry -- and
writes the plugin folder Dresscode expects:

    <Plugin>/<Plugin>.uplugin
    <Plugin>/Resources/Icon128.png                       (when a picture given)
    <Plugin>/Content/Paks/WindowsNoEditor/<Plugin>End-WindowsNoEditor.*

EVERY package is renamed into the plugin's namespace -- all thirteen real
Dresscode mods surveyed contain not one package outside their own root, and
a container that broke that rule was fatal at startup when mounted as a
plugin. The one thing this costs: a loose pak that deliberately overrode
stock packages beyond its mesh (extra skin textures, say) loses those
overrides, because an override IS its stock ID. That loss is inherent to the
destination format; Dresscode authors ship such textures re-cooked under the
plugin root too.
"""

import base64
import hashlib
import json
import os
import struct
import zlib

import assetreg
import cityhash
import conheader
import dirindex
import iostore
import mkpkg
import moddata
import pakfile
import pkgedit
import pngfile
import rename
import tagged
import writer
from zen import ZenPackage

META_PKG = "/FF7RML/ModLoaders/Structs/PDA_ModMetaData"
CHAR_PKG = ("/FF7RML/ModLoaders/Extensions/FF7RDataLibrary/Structs/"
            "PDA_ModData_Character")

# Struct GUIDs from the FF7RML release build -- properties of ITS structs,
# identical for every mod cooked against it. The field names beside them are
# re-read from the installed loader when possible (see fields_from_ff7rml).
GUID_META = bytes.fromhex("86a6ea87b2a64840a5f5e7bd75c6d3bf")
GUID_CHAR = bytes.fromhex("44bfa0c3bd4d8c499068252f8d9407a7")
GUID_GENERAL = bytes.fromhex("6c3f16caa4fdf64ca2a9736a01fdc9ba")
GUID_SKM = bytes.fromhex("2dd4b5d0d0e955439059cea8fbc1af19")
GUID_CUSTOM = bytes.fromhex("d15d966e403cbf42a1d70c2f9b2918cb")

FIELDS = {
    "FriendlyName": "FriendlyName_12_C07EA6A34E4FFF2BAB8F1BA65AC4A8DF",
    "Description": "Description_13_A25F0843431EACF0BC46EDB5730614F7",
    "Thumbnail": "Thumbnail_41_671E8B53470D848DCAF860BDCD707C79",
    "Category": "Category_14_5E073DB04D88E44458A795B8456DC342",
    "CreatedBy": "CreatedBy_15_E789BC57487D4B72CBC323951F03CCB8",
    "CreatedByURL": "CreatedByURL_16_A60FA18544C00457DC507194F14ED693",
    "GeneralData": "GeneralData_50_E3B347814D726EDE0AD7349893E85FE6",
    "Name": "Name_10_E3B347814D726EDE0AD7349893E85FE6",
    "OutfitDescription": "Description_35_252A1F9D4409B960C691ABA7A5C3C59D",
    "PreviewImage": "PreviewImage_47_815D57BE4E543D697C6FE09CFCA8AA8B",
    "SkeletalMeshData": "SkeletalMeshData_53_252A1F9D4409B960C691ABA7A5C3C59D",
    "PlayerType": "PlayerType_46_64A7C9EB4807B77D71F279B1BC6B83AD",
    "SkeletalMesh": "SkeletalMesh_47_99589AB142F64535793B99B657B24925",
    "Actor": "Actor_48_3E9EEC7E463CF3CE2E03A593E2A8E05A",
    "AdditionalData": "AdditionalData_52_64A7C9EB4807B77D71F279B1BC6B83AD",
    "DataAssets": "DataAssets_36_F06B7E4046D8297A3EE532964155B1F9",
    "CustomData": "CustomData_43_F94AEDDF476C152510C1BA8B76D6E68F",
}


def fields_from_ff7rml():
    """
    Refresh FIELDS from the installed loader's own struct definitions --
    blueprint field names carry a GUID suffix tied to the FF7RML build, so
    the source of truth is the loader, never a snapshot and never another
    author's asset. Falls back silently to the snapshot when FF7RML is not
    installed (converting on a machine without the game).
    """
    import config
    utoc = os.path.join(getattr(config, "MODS_DIR", ""), "FF7RML", "Content",
                        "Paks", "WindowsNoEditor",
                        "FF7RMLEnd-WindowsNoEditor.utoc")
    if not os.path.exists(utoc):
        return
    bases = {tagged.base_field(v): k for k, v in FIELDS.items()}
    try:
        toc = iostore.Toc(utoc)
        for i, p in toc.paths.items():
            if "/Structs/UDS_" not in "/" + p.replace("\\", "/"):
                continue
            for n in ZenPackage(toc.read(i)).names:
                base = tagged.base_field(n)
                if base in bases and n != base:
                    FIELDS[bases[base]] = n
        toc.close()
    except Exception:
        pass                            # snapshot stays; build remains valid


# Tag set every cooked preview texture carries, copied from real registries.
def texture_tags(width, height):
    return [
        ("Format", "unknown"), ("SRGB", "True"),
        ("CompressionSettings", "TC_Default"), ("MipLoadOptions", "Default"),
        ("LODBias", "0"), ("Filter", "TF_Default"),
        ("Dimensions", f"{width}x{height}"), ("NeverStream", "True"),
        ("VirtualTextureStreaming", "False"),
        ("LODGroup", "TEXTUREGROUP_World"), ("AddressY", "TA_Wrap"),
        ("AddressX", "TA_Wrap"), ("HasAlphaChannel", "True"),
    ]


def data_asset_tags(class_obj, class_pkg, asset_name):
    return [
        ("PrimaryAssetType", class_obj),
        ("PrimaryAssetName", asset_name),
        ("AssetBundleData", "(Bundles=)"),
        ("NativeClass",
         f"BlueprintGeneratedClass'{class_pkg}.{class_obj}'"),
    ]


ACCESS_INI = b"[AccessTransformers]\r\n\r\n"
SETTINGS_INI = (b"[StageSettings]\r\n"
                b"+AdditionalNonUSFDirectories=Resources\r\n")

SKELETAL_MESH = cityhash.object_id("/Script/Engine", "SkeletalMesh", 1)


def find_stock_mesh(packages):
    """(package name, player key) of the stock costume mesh this loose pak
    overrides -- the package sitting on a known character's default slot."""
    for info in packages.values():
        low = info["name"].lower()
        for key, (prefix, folder) in moddata.PLAYER_TYPES.items():
            stock = f"/game/character/player/{folder.lower()}/model/{prefix.lower()}_00"
            if low == stock:
                return info["name"], key
    return None, None


def mesh_object_name(toc, chunk):
    """The SkeletalMesh export's object name -- what a soft path points at."""
    z = ZenPackage(toc.read(chunk))
    for e in z.exports:
        if e["cls"] == SKELETAL_MESH:
            return e["name"]
    return z.exports[0]["name"]


def safe_id(text, used, fallback):
    cleaned = "".join(c for c in text if c.isalnum()) or fallback
    out, n = cleaned, 1
    while out.lower() in used:
        n += 1
        out = f"{cleaned}{n}"
    used.add(out.lower())
    return out


def arc_positions(data):
    """Byte offset of every graph arc's FromExportBundleIndex, in order.

    Ordinals over this list identify an arc independent of the name table's
    size, which is what lets the round trip put back the -1 arcs the loose
    conversion had to flatten (the ~mods loader rejects them; a plugin
    container carries them fine). Bounded EXACTLY like pkgedit's fix-arcs
    walk, so ordinal K here is the arc that walk would have touched."""
    z = ZenPackage(data)
    end = z.graph_off + z.graph_size
    out = []
    if z.graph_size < 4:
        return out
    o = z.graph_off
    count = struct.unpack_from("<I", data, o)[0]
    o += 4
    for _ in range(count):
        if o + 12 > end:
            break
        o += 8
        narcs = struct.unpack_from("<I", data, o)[0]
        o += 4
        for _ in range(narcs):
            if o + 8 > end:
                break
            out.append(o)
            o += 8
    return out


def original_blocks(toc, index):
    """A chunk's compressed blocks exactly as stored -- reusing them keeps
    untouched data byte-identical instead of re-Oodled."""
    offset, length = toc.offlen[index]
    b = offset // toc.block_size
    out, remaining = [], length
    while remaining > 0:
        pos, csize, usize, method = toc.blocks[b]
        toc.ucas.seek(pos)
        out.append((toc.ucas.read(csize), usize, method))
        remaining -= usize
        b += 1
    return out


def restore(rt, parts, out_root, optionals=None, say=print):
    """
    Rebuild the ORIGINAL Dresscode mod these loose paks came from, from the
    record their dresscode.json carries. Every package returns to its
    original name and bytes, the registration assets, toggle blueprints and
    material packs drop in verbatim, Optional paks give their overrides
    back, and the container keeps the original chunk order, load order and
    header shape. Only recompressed streams can differ from the original
    files -- same content, same checksums, different Oodle output.

    `parts`: {outfit folder ("." for the root): its .utoc path};
    `optionals`: {folder: .utoc path} for the extras in the Optional folders.
    """
    plugin = rt["plugin"]
    cid = int(rt["cid"])
    mount = rt["mount"]
    ent = rt.get("entries", {})

    def recorded(name):
        """(exp, bun, deps) from the record, or None on a legacy record."""
        e = ent.get(name)
        if e and len(e) >= 5:
            return e[2], e[3], [int(d) for d in e[4]]
        return None

    sources = []
    for rel, vinfo in rt["variants"].items():
        utoc = parts.get(rel)
        if not utoc:
            raise RuntimeError(f"an outfit folder is missing: {rel!r} -- "
                               "delete dresscode.json to rebuild fresh")
        sources.append((rel, vinfo, utoc, False))
    for rel, vinfo in (rt.get("optionals") or {}).items():
        utoc = (optionals or {}).get(rel)
        if not utoc:
            raise RuntimeError(
                f"an extra's paks are missing: {rel!r} -- "
                "delete dresscode.json to rebuild fresh (without toggles)")
        sources.append((rel, vinfo, utoc, True))

    merged = {}         # original pid -> record
    tocs = []
    template_toc = None
    # Optional paks name objects their base pak serves; the variants know
    # what those are called on the way back, the optionals do not.
    extra_imports = {}
    for rel, vinfo, utoc, is_optional in sources:
        toc = iostore.Toc(utoc)
        tocs.append(toc)
        if template_toc is None:
            template_toc = toc
        packages = rename.read_packages(toc)
        renames = {k: v for k, v in vinfo["renames_back"].items()}
        objects = {k: dict(v) for k, v in vinfo["objects_back"].items()}
        new_data, _new_ids = rename.rewrite_chunks(
            toc, packages, renames, object_renames=objects,
            extra_imports=extra_imports if is_optional else None)
        if not is_optional:
            for pid, p in packages.items():
                old = p["name"]
                new = renames.get(old.lower())
                if new and new != old:
                    for path in p["exports"]:
                        extra_imports[cityhash.object_id(old, path)] = \
                            cityhash.object_id(new, path)
        pid_map = {pid: cityhash.package_id(renames[p["name"].lower()])
                   for pid, p in packages.items()
                   if p["name"].lower() in renames}

        # The forward export rename APPENDED the stock object's name so the
        # table's indices stayed valid; renaming back orphans it at the
        # tail. Drop it, or the package comes back a few bytes bigger than
        # it started.
        by_low = {p["name"].lower(): p for p in packages.values()}
        for pkg_low, m in objects.items():
            pkg = by_low.get(pkg_low)
            if not pkg or pkg["chunk"] not in new_data:
                continue
            d = new_data[pkg["chunk"]]
            z = ZenPackage(d)
            stock = set(m)
            while z.names and z.names[-1] in stock:
                gone = len(z.names) - 1
                used = {e["name"] for e in z.exports}
                if z.names[gone] in used:
                    break
                d = pkgedit.rewrite(d, names=z.names[:gone],
                                    allow_shrink=True)
                z = ZenPackage(d)
            new_data[pkg["chunk"]] = d

        hdr = next(toc.read(i) for i in range(toc.n)
                   if toc.chunk_ids[i][11] == 10)
        info = conheader.parse(hdr)
        entry_meta = {}
        for j, pid in enumerate(conheader.package_ids(hdr, info)):
            _sz, exp, bun = struct.unpack_from(
                "<Qii", hdr, info["store_off"] + j * 32)[:3]
            entry_meta[pid] = (exp, bun, [
                pid_map.get(p, p)
                for p in conheader.imported_packages(hdr, info, j)])

        bulks = {}
        for i in range(toc.n):
            t = toc.chunk_ids[i][11]
            if t in (3, 4):
                owner = int.from_bytes(toc.chunk_ids[i][:8], "little")
                owner = pid_map.get(owner, owner)
                cid12 = owner.to_bytes(8, "little") + toc.chunk_ids[i][8:]
                bulks.setdefault(owner, []).append((cid12, t, toc, i))

        neg = {k.lower(): v for k, v in rt.get("neg_arcs", {}).items()}
        for pid, pkg in packages.items():
            new_pid = pid_map.get(pid, pid)
            name = renames.get(pkg["name"].lower(), pkg["name"])
            i = pkg["chunk"]
            changed = i in new_data
            payload = new_data[i] if changed else toc.read(i)
            ordinals = neg.get(name.lower())
            if ordinals:
                buf = bytearray(payload)
                pos = arc_positions(buf)
                for k in ordinals:
                    struct.pack_into("<i", buf, pos[k], -1)
                payload, changed = bytes(buf), True
            have = merged.get(new_pid)
            if have is not None:
                # Optional paks carry derived copies; the variants (and the
                # stored bytes, which land last and always win) are the
                # authority.
                if is_optional:
                    continue
                if have["payload"] != payload:
                    raise RuntimeError(
                        f"outfits disagree about {name} -- the folders were "
                        "not converted from the same mod")
                continue
            # The ORIGINAL header entry when the record has it; harvesting
            # from the loose header is only for older records, and loses
            # dependencies on dropped packages.
            rec = recorded(name)
            exp, bun, pdeps = rec if rec else entry_meta.get(pid, (1, 1, []))
            merged[new_pid] = dict(
                name=name, payload=payload,
                src=None if changed else (toc, i),
                deps=pdeps, exp=exp, bun=bun, bulks=bulks.get(new_pid, []))

    stored = rt.get("stored_chunks")
    legacy = rt.get("reg_chunks", {})
    if stored is None:
        stored = {k: v["data"] for k, v in legacy.items()}
    for name, blob in stored.items():
        data = zlib.decompress(base64.b64decode(blob))
        rec = recorded(name)
        if rec is None and name in legacy:
            rec = (legacy[name]["exp"], legacy[name]["bun"],
                   [int(d) for d in legacy[name]["deps"]])
        exp, bun, pdeps = rec if rec else (1, 1, [])
        merged[cityhash.package_id(name)] = dict(
            name=name, payload=data, src=None,
            deps=pdeps, exp=exp, bun=bun, bulks=[])

    # ---- header chunk in the original's exact shape ----
    id_order = rt["id_order"]
    by_pid = {cityhash.package_id(n): n for n in id_order}
    missing = [n for n in id_order
               if cityhash.package_id(n) not in merged]
    if missing:
        raise RuntimeError(
            "these packages of the original mod are gone from the loose "
            "paks: " + ", ".join(missing[:4])
            + " -- delete dresscode.json to rebuild fresh")

    hdr_out = struct.pack("<QIIIIQ", cid, len(id_order), 0, 0, 8, 0xC1640000)
    hdr_out += struct.pack("<I", len(id_order))
    hdr_out += b"".join(cityhash.package_id(n).to_bytes(8, "little")
                        for n in id_order)
    store = bytearray()
    for j, n in enumerate(id_order):
        rec = merged[cityhash.package_id(n)]
        e = ent.get(n) or [j, -1]
        store += struct.pack("<QiiiI", len(rec["payload"]), rec["exp"],
                             rec["bun"], e[0], e[1] & 0xFFFFFFFF)
        store += struct.pack("<II", 0, 0)
    for j, n in enumerate(id_order):
        rec = merged[cityhash.package_id(n)]
        view = j * 32 + 24
        if rec["deps"]:
            struct.pack_into("<II", store, view, len(rec["deps"]),
                             len(store) - view)
            store += struct.pack(f"<{len(rec['deps'])}Q", *rec["deps"])
    hdr_out += struct.pack("<I", len(store)) + store
    if len(hdr_out) > rt["hdr_len"]:
        raise RuntimeError("restore: header grew past the original -- "
                           "delete dresscode.json to rebuild fresh")
    hdr_out += b"\0" * (rt["hdr_len"] - len(hdr_out))

    # ---- chunks in the original order ----
    comp = next((m for m, nm in enumerate(template_toc.methods)
                 if nm.lower() == "oodle"), None)

    def fresh_blocks(payload):
        return rename.pack_blocks(payload, template_toc.block_size, comp)

    chunks, payloads, paths = [], [], []
    pending_bulks = {pid: list(rec["bulks"]) for pid, rec in merged.items()}
    prefix = f"/{plugin}/"
    for name, t in rt["chunk_order"]:
        if t == 10:
            chunks.append(dict(id=cid.to_bytes(8, "little") + b"\0\0\0\x0a",
                               blocks=fresh_blocks(hdr_out),
                               size=len(hdr_out)))
            payloads.append(hdr_out)
            continue
        pid = cityhash.package_id(name)
        rec = merged[pid]
        rel = rec["name"][len(prefix):] \
            if rec["name"].lower().startswith(prefix.lower()) \
            else rec["name"].lstrip("/")
        if t == 2:
            blocks = original_blocks(*rec["src"]) if rec["src"] \
                else fresh_blocks(rec["payload"])
            chunks.append(dict(id=pid.to_bytes(8, "little") + b"\0\0\0\x02",
                               blocks=blocks, size=len(rec["payload"])))
            payloads.append(rec["payload"])
            paths.append((rel + ".uasset", len(chunks) - 1))
        else:
            queue = pending_bulks.get(pid, [])
            k = next((x for x, (_c, bt, _t, _i) in enumerate(queue)
                      if bt == t), None)
            if k is None:
                raise RuntimeError(f"restore: bulk data missing for {name}")
            cid12, _bt, btoc, bi = queue.pop(k)
            data = btoc.read(bi)
            chunks.append(dict(id=bytes(cid12),
                               blocks=original_blocks(btoc, bi),
                               size=len(data)))
            payloads.append(data)
            paths.append((rel + (".uptnl" if t == 4 else ".ubulk"),
                          len(chunks) - 1))

    directory = dirindex.build_dir_index(mount, paths)
    body, ucas, _offlen, block_table = writer.build_container(
        template_toc, chunks, template_toc.block_size)
    head = bytearray(writer.build_toc_header(
        template_toc, len(chunks), len(block_table), len(directory),
        template_toc.block_size))
    struct.pack_into("<Q", head, 0x38, cid)
    metas = b"".join(hashlib.sha1(p).digest() + b"\0" * 12 + b"\x01"
                     for p in payloads)

    pak_dir = os.path.join(out_root, plugin, "Content", "Paks",
                           "WindowsNoEditor")
    os.makedirs(pak_dir, exist_ok=True)
    base = f"{plugin}End-WindowsNoEditor"
    with open(os.path.join(pak_dir, base + ".utoc"), "wb") as f:
        f.write(bytes(head) + bytes(body) + directory + metas)
    with open(os.path.join(pak_dir, base + ".ucas"), "wb") as f:
        f.write(ucas)

    pak_files = [(p, zlib.decompress(base64.b64decode(z)))
                 for p, z in rt["pak_files"]]
    pak = pakfile.build_plugin(rt["pak_mount"], pak_files,
                               compressor=iostore.oodle_compress,
                               seed=int(rt["pak_seed"]))
    with open(os.path.join(pak_dir, base + ".pak"), "wb") as f:
        f.write(pak)

    root = os.path.join(out_root, plugin)
    with open(os.path.join(root, f"{plugin}.uplugin"), "wb") as f:
        f.write(base64.b64decode(rt["uplugin"]))
    if rt.get("icon_md5"):
        # icon.png beside the template IS the original Icon128.png.
        rel0 = next(iter(rt["variants"]))
        src_root = os.path.dirname(parts[rel0]) if rel0 == "." \
            else os.path.dirname(os.path.dirname(parts[rel0]))
        icon = os.path.join(src_root, "icon.png")
        if os.path.exists(icon):
            os.makedirs(os.path.join(root, "Resources"), exist_ok=True)
            with open(icon, "rb") as s, \
                    open(os.path.join(root, "Resources", "Icon128.png"),
                         "wb") as d:
                d.write(s.read())

    for toc in tocs:
        toc.close()
    say(f"    restored  {plugin}{os.sep}  "
        f"({len(chunks)} chunks, {len(ucas) / (1024 * 1024):,.1f} MB, "
        "original layout)")
    return root


def build(meta, outfits, plugin, out_root, say=print):
    """
    Write the plugin folder. `meta` and `outfits` come from the dresscode.json
    template (read_template's output shapes); each outfit carries its utoc.
    Returns the plugin folder path.
    """
    fields_from_ff7rml()
    F = FIELDS
    cid = cityhash.package_id(f"{plugin}End")
    mount = f"../../../End/Mods/{plugin}/Content/"

    merged = {}                 # pid -> dict(name, data, deps, exp, bun, bulks)
    template_toc = None
    used_names = set()
    entries_outfits = []        # per outfit: (mesh soft path, player key)

    tocs = []
    for k, outfit in enumerate(outfits):
        toc = iostore.Toc(outfit["utoc"])
        tocs.append(toc)
        if template_toc is None:
            template_toc = toc
        packages = rename.read_packages(toc)
        mesh_name, player = find_stock_mesh(packages)
        if not mesh_name:
            raise RuntimeError(
                f"{os.path.basename(outfit['utoc'])} does not replace any "
                "character's standard costume -- only costume mods convert")

        safe = safe_id(outfit["name"], used_names, f"Outfit{k + 1}")
        mesh_new = f"/{plugin}/Outfits/{safe}"
        renames = {mesh_name.lower(): mesh_new}
        for pkg in packages.values():
            low = pkg["name"].lower()
            if low in renames or low.startswith(f"/{plugin.lower()}/"):
                continue
            tail = pkg["name"][6:] if low.startswith("/game/") \
                else pkg["name"].lstrip("/")
            renames[low] = f"/{plugin}/{tail}"
        new_data, new_ids = rename.rewrite_chunks(
            toc, packages, renames, fix_arcs=True)

        hdr = next(toc.read(i) for i in range(toc.n)
                   if toc.chunk_ids[i][11] == 10)
        info = conheader.parse(hdr)
        ids = conheader.package_ids(hdr, info)
        old_mesh_pid = cityhash.package_id(mesh_name)
        pid_map = {pid: cityhash.package_id(renames[pkg["name"].lower()])
                   for pid, pkg in packages.items()
                   if pkg["name"].lower() in renames}
        entry_meta = {}
        for j, pid in enumerate(ids):
            _sz, exp, bun = struct.unpack_from(
                "<Qii", hdr, info["store_off"] + j * 32)[:3]
            entry_meta[pid] = (exp, bun, [
                pid_map.get(p, p)
                for p in conheader.imported_packages(hdr, info, j)])

        bulks = {}
        for i in range(toc.n):
            if toc.chunk_ids[i][11] in (3, 4):
                cid12 = new_ids.get(i, toc.chunk_ids[i])
                bulks.setdefault(
                    int.from_bytes(cid12[:8], "little"), []).append(
                        (cid12, new_data.get(i) or toc.read(i)))

        for pid, pkg in packages.items():
            i = pkg["chunk"]
            new_pid = pid_map.get(pid, pid)
            data = new_data.get(i) or toc.read(i)
            name = renames.get(pkg["name"].lower(), pkg["name"])
            exp, bun, pdeps = entry_meta.get(pid, (1, 1, []))
            have = merged.get(new_pid)
            if have is not None:
                if len(have["data"]) != len(data) or have["data"] != data:
                    raise RuntimeError(
                        f"outfits disagree about {name} -- the variant "
                        "folders were built from different mods")
                continue
            merged[new_pid] = dict(name=name, data=data, deps=pdeps,
                                   exp=exp, bun=bun,
                                   bulks=bulks.get(new_pid, []))

        obj = mesh_object_name(toc, packages[old_mesh_pid]["chunk"])
        entries_outfits.append((outfit, f"{mesh_new}.{obj}", player))

    # ---- synthesized packages ------------------------------------------
    def add(name, data, deps):
        pid = cityhash.package_id(name)
        merged[pid] = dict(name=name, data=data, deps=deps, exp=1, bun=1,
                           bulks=[])
        return pid

    registry_assets = []
    previews = []
    for k, (outfit, _mesh, _player) in enumerate(entries_outfits):
        if not outfit.get("preview"):
            previews.append(None)
            continue
        w, h, bgra = pngfile.decode(outfit["preview"])
        name = f"/{plugin}/Previews/Preview{k + 1}"
        add(name, mkpkg.build_texture(
            name, f"Preview{k + 1}", w, h, bgra,
            lighting_guid=hashlib.md5(bgra).digest()), [])
        previews.append(f"{name}.Preview{k + 1}")
        registry_assets.append(dict(
            object_path=f"{name}.Preview{k + 1}",
            package_path=f"/{plugin}/Previews", class_name="Texture2D",
            package_name=name, asset_name=f"Preview{k + 1}",
            tags=texture_tags(w, h)))

    thumb_path = None
    if meta.get("icon"):
        w, h, bgra = pngfile.decode(meta["icon"])
        name = f"/{plugin}/MetaData/Thumbnail"
        add(name, mkpkg.build_texture(
            name, "Thumbnail", w, h, bgra,
            lighting_guid=hashlib.md5(bgra).digest()), [])
        thumb_path = name
        registry_assets.append(dict(
            object_path=f"{name}.Thumbnail",
            package_path=f"/{plugin}/MetaData", class_name="Texture2D",
            package_name=name, asset_name="Thumbnail",
            tags=texture_tags(w, h)))

    NULL = cityhash.NULL_INDEX
    meta_imports = [cityhash.object_id(META_PKG, "PDA_ModMetaData_C"), NULL,
                    NULL,
                    cityhash.object_id(META_PKG, "Default__PDA_ModMetaData_C")]
    meta_graph = [(cityhash.package_id(META_PKG), [(0, 0)])]
    meta_deps = [cityhash.package_id(META_PKG)]
    extra = []
    thumb_ref = ("obj", 0)
    if thumb_path:
        meta_imports.append(cityhash.object_id(thumb_path, "Thumbnail"))
        meta_graph.append((cityhash.package_id(thumb_path), [(0, 0)]))
        meta_deps.append(cityhash.package_id(thumb_path))
        extra = [thumb_path, "Thumbnail", "Texture2D"]
        thumb_ref = ("obj", -len(meta_imports))
    meta_props = [
        ("MetaData", ("struct", "UDS_ModMetaData", GUID_META, [
            (F["FriendlyName"], ("str", meta["name"])),
            (F["Description"], ("str", meta["description"])),
            (F["Thumbnail"], thumb_ref),
            (F["Category"], ("str", meta["category"])),
            (F["CreatedBy"], ("str", meta["author"])),
            (F["CreatedByURL"], ("str", "")),
        ])),
        ("NativeClass", ("obj", -1)),
    ]
    da_name = f"/{plugin}/MetaData/DA_ModMetaData"
    add(da_name, mkpkg.build(
        da_name, "DA_ModMetaData", META_PKG, "PDA_ModMetaData_C",
        meta_props, imports=meta_imports, graph=meta_graph,
        extra_names=extra), meta_deps)
    registry_assets.append(dict(
        object_path=f"{da_name}.DA_ModMetaData",
        package_path=f"/{plugin}/MetaData",
        class_name="PDA_ModMetaData_C", package_name=da_name,
        asset_name="DA_ModMetaData",
        tags=data_asset_tags("PDA_ModMetaData_C", META_PKG,
                             "DA_ModMetaData")))

    bodies = []
    for k, (outfit, mesh_path, player) in enumerate(entries_outfits):
        bodies.append([
            (F["GeneralData"], ("struct", "UDS_ModData_General", GUID_GENERAL, [
                (F["Name"], ("str", outfit["name"])),
                (F["OutfitDescription"], ("str", outfit["description"])),
                (F["PreviewImage"], ("softpath", previews[k])),
            ])),
            (F["SkeletalMeshData"],
             ("struct", "UDS_AssetType_SkeletalMesh", GUID_SKM, [
                 (F["PlayerType"],
                  ("enum", "EPlayerType", f"EPlayerType::{player}")),
                 (F["SkeletalMesh"], ("softpath", mesh_path)),
                 (F["Actor"], ("softpath", None)),
             ])),
            (F["AdditionalData"], ("struct", "UDS_AssetType_Custom",
                                   GUID_CUSTOM, [
                (F["DataAssets"], ("map", "NameProperty", "ObjectProperty")),
                (F["CustomData"], ("map", "NameProperty", "StructProperty")),
            ])),
        ])
    char_props = [
        ("Character Data",
         ("array_structs", "UDS_ModData_Character", GUID_CHAR, bodies)),
        ("NativeClass", ("obj", -1)),
    ]
    char_name = f"/{plugin}/MetaData/CharacterData"
    add(char_name, mkpkg.build(
        char_name, "CharacterData", CHAR_PKG, "PDA_ModData_Character_C",
        char_props,
        imports=[cityhash.object_id(CHAR_PKG, "PDA_ModData_Character_C"),
                 NULL,
                 cityhash.object_id(CHAR_PKG,
                                    "Default__PDA_ModData_Character_C")],
        graph=[(cityhash.package_id(CHAR_PKG), [(0, 0)])]),
        [cityhash.package_id(CHAR_PKG)])
    registry_assets.append(dict(
        object_path=f"{char_name}.CharacterData",
        package_path=f"/{plugin}/MetaData",
        class_name="PDA_ModData_Character_C", package_name=char_name,
        asset_name="CharacterData",
        tags=data_asset_tags("PDA_ModData_Character_C", CHAR_PKG,
                             "CharacterData")))

    # ---- container header chunk ----------------------------------------
    order = sorted(merged)
    hdr = struct.pack("<QIIIIQ", cid, len(order), 0, 0, 8, 0xC1640000)
    hdr += struct.pack("<I", len(order))
    hdr += struct.pack(f"<{len(order)}Q", *order)
    store = bytearray()
    for j, pid in enumerate(order):
        rec = merged[pid]
        # LoadOrder is any dense permutation; the field after it is 0xFFFFFFFF
        # in every donor mod AND every CE-cooked loose pak -- never 0.
        store += struct.pack("<QiiII", len(rec["data"]), rec["exp"],
                             rec["bun"], j, 0xFFFFFFFF)
        store += struct.pack("<II", 0, 0)            # views filled below
    for j, pid in enumerate(order):
        rec = merged[pid]
        view = j * 32 + 24
        if rec["deps"]:
            struct.pack_into("<II", store, view, len(rec["deps"]),
                             len(store) - view)
            store += struct.pack(f"<{len(rec['deps'])}Q", *rec["deps"])
    hdr += struct.pack("<I", len(store)) + store
    if len(hdr) % 65536:                             # the block-size invariant
        hdr += b"\0" * (65536 - len(hdr) % 65536)

    # ---- chunk list and files --------------------------------------------
    comp = next((m for m, n in enumerate(template_toc.methods)
                 if n.lower() == "oodle"), None)

    def blocks_of(payload):
        return rename.pack_blocks(payload, template_toc.block_size, comp)

    # Packages first, bulk data after -- every container observed, the
    # game's own included, keeps the two segregated.
    chunks = [dict(id=cid.to_bytes(8, "little") + b"\0\0\0\x0a",
                   blocks=blocks_of(hdr), size=len(hdr))]
    payloads = [hdr]
    paths = []
    prefix = f"/{plugin}/"
    rels = {}
    for pid in order:
        rec = merged[pid]
        cid12 = pid.to_bytes(8, "little") + b"\0\0\0\x02"
        chunks.append(dict(id=cid12, blocks=blocks_of(rec["data"]),
                           size=len(rec["data"])))
        payloads.append(rec["data"])
        # Every package was renamed under /<plugin>/ (or already lived
        # there), so stripping the fixed-length prefix is always right.
        rels[pid] = rec["name"][len(prefix):]
        paths.append((rels[pid] + ".uasset", len(chunks) - 1))
    for pid in order:
        for bid, bdata in merged[pid]["bulks"]:
            chunks.append(dict(id=bytes(bid), blocks=blocks_of(bdata),
                               size=len(bdata)))
            payloads.append(bdata)
            ext = ".uptnl" if bid[11] == 4 else ".ubulk"
            paths.append((rels[pid] + ext, len(chunks) - 1))

    directory = dirindex.build_dir_index(mount, paths)
    body, ucas, _offlen, block_table = writer.build_container(
        template_toc, chunks, template_toc.block_size)
    head = bytearray(writer.build_toc_header(
        template_toc, len(chunks), len(block_table), len(directory),
        template_toc.block_size))
    struct.pack_into("<Q", head, 0x38, cid)
    metas = b"".join(hashlib.sha1(p).digest() + b"\0" * 12 + b"\x01"
                     for p in payloads)

    pak_dir = os.path.join(out_root, plugin, "Content", "Paks",
                           "WindowsNoEditor")
    os.makedirs(pak_dir, exist_ok=True)
    base = f"{plugin}End-WindowsNoEditor"
    with open(os.path.join(pak_dir, base + ".utoc"), "wb") as f:
        f.write(bytes(head) + bytes(body) + directory + metas)
    with open(os.path.join(pak_dir, base + ".ucas"), "wb") as f:
        f.write(ucas)

    registry = assetreg.build(registry_assets)
    pak = pakfile.build_plugin(
        f"../../../End/Mods/{plugin}/",
        [("AssetRegistry.bin", registry),
         ("Config/AccessTransformers.ini", ACCESS_INI),
         ("Config/PluginSettings.ini", SETTINGS_INI)],
        compressor=iostore.oodle_compress)
    with open(os.path.join(pak_dir, base + ".pak"), "wb") as f:
        f.write(pak)

    uplugin = {
        "FileVersion": 3, "Version": 1,
        "VersionName": meta["version"], "FriendlyName": meta["name"],
        "Description": meta["description"], "Category": "Modding",
        "CreatedBy": meta["author"], "CreatedByURL": "",
        "DocsURL": "", "MarketplaceURL": "", "SupportURL": "",
        "CanContainContent": True, "IsBetaVersion": False,
        "IsExperimentalVersion": False, "Installed": False,
    }
    root = os.path.join(out_root, plugin)
    with open(os.path.join(root, f"{plugin}.uplugin"), "w",
              encoding="utf-8") as f:
        json.dump(uplugin, f, indent="\t")
    if meta.get("icon") and meta["icon"].lower().endswith(".png"):
        os.makedirs(os.path.join(root, "Resources"), exist_ok=True)
        with open(meta["icon"], "rb") as src, \
                open(os.path.join(root, "Resources", "Icon128.png"),
                     "wb") as dst:
            dst.write(src.read())

    for toc in tocs:
        toc.close()                     # Windows holds temp folders hostage
    say(f"    written  {plugin}{os.sep}  "
        f"({len(chunks)} chunks, {len(ucas) / (1024 * 1024):,.1f} MB)")
    return root
