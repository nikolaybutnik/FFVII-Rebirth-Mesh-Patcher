"""
mkdc.py -- assembles a complete Dresscode plugin from pak mods.

The forward conversion's mirror. Takes one pak per outfit, renames each
outfit's mesh out of the stock costume slot into the plugin's namespace,
merges everything into one container, synthesizes what a pak never had
-- the two registration assets, preview textures, the AssetRegistry -- and
writes the plugin folder Dresscode expects:

    <Plugin>/<Plugin>.uplugin
    <Plugin>/Resources/Icon128.png                       (when a picture given)
    <Plugin>/Content/Paks/WindowsNoEditor/<Plugin>End-WindowsNoEditor.*

EVERY package is renamed into the plugin's namespace -- none of 95 real
Dresscode containers surveyed holds one package outside its own root, and a
container that broke that rule was fatal at startup when mounted as a
plugin. A pak that deliberately overrode stock packages beyond its
mesh (retouched skin textures, say) would lose those overrides to the
rename, because an override IS its stock ID -- stockgraft.py keeps them
working by carrying the stock materials that sample them.
"""

import base64
import hashlib
import json
import os
import re
import struct
import sys
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
import stockgraft
import toggles
import tagged
import weapons
import writer
from zen import ZenPackage

# Every row of every real mod names a picture -- none is ever left empty, and
# the loader ships this one for rows an author gave no icon of their own.
TEMPLATE_ICON = ("/FF7RML/UI/Dresscode_TemplateIcon_Sprite"
                 ".Dresscode_TemplateIcon_Sprite")

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


# Which loader struct owns which FIELDS keys. Several structs share field
# base names -- every UDS_ModData_* carries a Name and a Description, and
# UDS_ModData_Object an Actor of its own -- so a field may only ever be
# refreshed from the one struct our rows are actually built against.
# Matching on base name across all structs once put UDS_ModData_Object's
# Actor into every toggle row: the game read no actor at all, and applying
# a toggle dressed the character in her default outfit.
STRUCT_FIELDS = {
    "UDS_ModMetaData": ("FriendlyName", "Description", "Thumbnail",
                        "Category", "CreatedBy", "CreatedByURL"),
    "UDS_ModData_General": ("Name", "OutfitDescription", "PreviewImage"),
    "UDS_ModData_Character": ("GeneralData", "SkeletalMeshData",
                              "AdditionalData"),
    "UDS_AssetType_SkeletalMesh": ("PlayerType", "SkeletalMesh", "Actor"),
    "UDS_AssetType_Custom": ("DataAssets", "CustomData"),
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
    try:
        toc = iostore.Toc(utoc)
        for i, p in toc.paths.items():
            leaf = os.path.splitext(
                os.path.basename(p.replace("\\", "/")))[0]
            owned = STRUCT_FIELDS.get(leaf)
            if not owned:
                continue
            bases = {tagged.base_field(FIELDS[k]): k for k in owned}
            for n in ZenPackage(toc.read(i)).names:
                k = bases.get(tagged.base_field(n))
                if k and n != tagged.base_field(n):
                    FIELDS[k] = n
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


# Any costume slot of a playable character: PC0003_07_Aerith_CostaClothing/
# Model/PC0003_07 as much as the _00 standard -- authors cook the same
# outfit over several slots, and which slot it came from stops mattering
# the moment it becomes a Dresscode costume.
_STOCK_SLOT = re.compile(
    r"^/game/character/player/(pc\d{4})_(\d{2})[^/]*/model/\1_\2$")


def find_stock_mesh(packages):
    """(package name, player key) of the stock costume mesh this pak
    overrides -- the package sitting on any known character's costume slot.

    A pak can cover several slots at once (a standard costume plus the
    changing-clothes story variant, say). The LOWEST slot is the costume worn
    in normal play, and picking by chunk order instead once handed the outfit
    row to the story variant."""
    prefixes = {prefix.lower(): key
                for key, (prefix, _folder) in moddata.PLAYER_TYPES.items()}
    hits = []
    for info in packages.values():
        m = _STOCK_SLOT.match(info["name"].lower())
        if m and m.group(1) in prefixes:
            hits.append((int(m.group(2)), info["name"], prefixes[m.group(1)]))
    if not hits:
        return None, None
    _slot, name, player = min(hits)
    return name, player


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


def mark_external_arcs(data, shipped):
    """
    Point every dependency arc on a package this container does NOT hold at
    bundle -1, and return the new bytes (None when nothing changed).

    The two formats are exact opposites here. A ~mods pak must not
    carry -1 arcs -- the vanilla loader silently refuses any package that
    does. A plugin must: every arc leaving a real Dresscode mod is -1, and
    an arc of 0 naming a package the container lacks sends the loader to a
    store entry that is not there, which it writes through anyway.
    """
    z = ZenPackage(data)
    if z.graph_size < 4:
        return None
    buf = bytearray(data)
    end = z.graph_off + z.graph_size
    o = z.graph_off
    count = struct.unpack_from("<I", buf, o)[0]
    o += 4
    changed = False
    for _ in range(count):
        if o + 12 > end:
            break
        pid, narcs = struct.unpack_from("<QI", buf, o)
        o += 12
        for _ in range(narcs):
            if o + 8 > end:
                break
            if pid not in shipped and \
                    struct.unpack_from("<I", buf, o)[0] != 0xFFFFFFFF:
                struct.pack_into("<I", buf, o, 0xFFFFFFFF)
                changed = True
            o += 8
    return bytes(buf) if changed else None


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
    Rebuild the ORIGINAL Dresscode mod these paks came from, from the
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
    stored_bulks = rt.get("stored_bulks") or {}
    for name, blob in stored.items():
        data = zlib.decompress(base64.b64decode(blob))
        rec = recorded(name)
        if rec is None and name in legacy:
            rec = (legacy[name]["exp"], legacy[name]["bun"],
                   [int(d) for d in legacy[name]["deps"]])
        exp, bun, pdeps = rec if rec else (1, 1, [])
        # A stored bulk rides as (cid12, type, None, payload) -- no source
        # container to reread it from.
        bulks = [(bytes.fromhex(cid_hex), bytes.fromhex(cid_hex)[11], None,
                  zlib.decompress(base64.b64decode(b)))
                 for cid_hex, b in stored_bulks.get(name, [])]
        merged[cityhash.package_id(name)] = dict(
            name=name, payload=data, src=None,
            deps=pdeps, exp=exp, bun=bun, bulks=bulks)

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
    dir_paths = rt.get("dir_paths") or []
    for k_ord, (name, t) in enumerate(rt["chunk_order"]):
        # The recorded index path keeps the cooker's file-name casing,
        # which a package's own name table may not share.
        path_rec = dir_paths[k_ord] if k_ord < len(dir_paths) else ""
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
            paths.append((path_rec or rel + ".uasset", len(chunks) - 1))
        else:
            queue = pending_bulks.get(pid, [])
            k = next((x for x, (_c, bt, _t, _i) in enumerate(queue)
                      if bt == t), None)
            if k is None:
                raise RuntimeError(f"restore: bulk data missing for {name}")
            cid12, _bt, btoc, bi = queue.pop(k)
            if btoc is None:                    # stored verbatim, bi = bytes
                data = bi
                blocks = fresh_blocks(data)
            else:
                data = btoc.read(bi)
                blocks = original_blocks(btoc, bi)
            chunks.append(dict(id=bytes(cid12), blocks=blocks,
                               size=len(data)))
            payloads.append(data)
            paths.append((path_rec or rel + (".uptnl" if t == 4 else
                                             ".ubulk"), len(chunks) - 1))

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
    if rt.get("icon_b64"):
        # A library mod's icon rides in its record verbatim -- it has no
        # extracted icon.png in the loose folder to read back.
        os.makedirs(os.path.join(root, "Resources"), exist_ok=True)
        with open(os.path.join(root, "Resources", "Icon128.png"), "wb") as f:
            f.write(base64.b64decode(rt["icon_b64"]))
    if rt.get("icon_md5"):
        # icon.png beside the template IS the original Icon128.png. Climb out
        # of the outfit's folder by the DEPTH of its recorded path, not a
        # fixed one level: outfits live under Variants\<name>\ now, and a
        # fixed guess quietly landed one folder short and lost the thumbnail.
        rel0 = next(iter(rt["variants"]))
        src_root = os.path.dirname(parts[rel0])
        if rel0 != ".":
            for _ in rel0.replace("\\", "/").split("/"):
                src_root = os.path.dirname(src_root)
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


def build(meta, outfits, plugin, out_root, say=print, extras=(),
          external=(), weapon_tiles=True):
    """
    Write the plugin folder. `meta` and `outfits` come from the dresscode.json
    template (read_template's output shapes); each outfit carries its utoc.

    `extras` is [(label, utoc)] for the old modular standard's optional paks:
    each becomes a toggle row per outfit it can act on. Weapon paks among
    them become weapons-menu tiles instead -- but only when `weapon_tiles`
    is set: a multi-outfit mod builds one plugin per outfit from the SAME
    extras, and only the first may carry the weapon tiles, or every plugin
    would register an identical row.

    `external` is lowercase package names to leave at their original /Game/
    paths and NOT ship: the stackable-masks design serves them from ~mods
    paks instead, so the old standard's drop-in override workflow keeps
    working while the outfit itself rides Dresscode. The plugin's materials
    import them across the container boundary (arcs -1, like any stock
    import).

    Returns the plugin folder path.
    """
    fields_from_ff7rml()
    F = FIELDS

    # A big mod is minutes of silent decompressing and recompressing; keep a
    # counter on screen so it reads as working, not hung. The counter line is
    # overwritten in place and cleared before any real message.
    progress = sys.stdout.isatty()

    def tick(msg):
        if progress:
            print(f"\r      {msg[:58]:<58}", end="", flush=True)

    def tick_done():
        if progress:
            print("\r" + " " * 66 + "\r", end="", flush=True)

    raw_say = say

    def say(msg):
        tick_done()
        raw_say(msg)

    cid = cityhash.package_id(f"{plugin}End")
    mount = f"../../../End/Mods/{plugin}/Content/"

    merged = {}                 # pid -> dict(name, data, deps, exp, bun, bulks)
    template_toc = None
    used_names = set()
    entries_outfits = []        # per outfit: (mesh soft path, player key)

    # Variant folders share most packages and they dedupe by name -- but
    # authors recook each variant separately, and a "shared" file whose
    # bytes came out different (the _Condition model, typically) cannot
    # live under one name. Find those up front; every outfit after the
    # first gets its own private copy of them.
    pre = []
    for outfit in outfits:
        tick(f"reading outfit {len(pre) + 1}/{len(outfits)}")
        ptoc = iostore.Toc(outfit["utoc"])
        pre.append((ptoc, rename.read_packages(ptoc)))
    shared = {}
    for k, (_ptoc, ppkgs) in enumerate(pre):
        for p in ppkgs.values():
            shared.setdefault(p["name"].lower(), []).append((k, p["chunk"]))
    diff_owner = {}
    mentions = {}       # identical shared name -> its lowercase name table
    for n_cmp, (low, where) in enumerate(shared.items()):
        tick(f"comparing shared files {n_cmp + 1}/{len(shared)}")
        if len(where) < 2:
            continue
        digests, first = set(), None
        for k, chunk in where:
            data = pre[k][0].read(chunk)
            if first is None:
                first = data
            digests.add(hashlib.md5(data).digest())
        if len(digests) > 1:
            diff_owner[low] = where[0][0]
        else:
            try:
                mentions[low] = {n.lower() for n in ZenPackage(first).names}
            except Exception:
                pass
    # Identical bytes are not enough: a package that references a name whose
    # rename differs per outfit (the mesh, or a private copy above) comes out
    # different once rewritten. Those need private copies too, transitively.
    divergent = set(diff_owner)
    for _ptoc, ppkgs in pre:
        mesh, _p = find_stock_mesh(ppkgs)
        if mesh:
            divergent.add(mesh.lower())
    changed = True
    while changed:
        changed = False
        for low, names in mentions.items():
            if low not in divergent and names & divergent:
                diff_owner[low] = shared[low][0][0]
                divergent.add(low)
                changed = True

    # Variant paks trigger the same graft notes; say each once, not per outfit.
    said = set()

    def say_once(msg):
        if msg not in said:
            said.add(msg)
            say(msg)

    tocs, wearers = [], []
    for k, outfit in enumerate(outfits):
        tick(f"merging outfit {k + 1}/{len(outfits)}: {outfit['name']}")
        toc, packages = pre[k]
        tocs.append(toc)
        if template_toc is None:
            template_toc = toc
        mesh_name, player = find_stock_mesh(packages)
        if not mesh_name:
            raise RuntimeError(
                f"{os.path.basename(outfit['utoc'])} does not replace any "
                "character's standard costume -- only costume mods convert")
        old_mesh_pid = cityhash.package_id(mesh_name)

        hdr = next(toc.read(i) for i in range(toc.n)
                   if toc.chunk_ids[i][11] == 10)
        info = conheader.parse(hdr)
        ids = conheader.package_ids(hdr, info)
        raw_meta = {}
        for j, pid in enumerate(ids):
            _sz, exp, bun = struct.unpack_from(
                "<Qii", hdr, info["store_off"] + j * 32)[:3]
            raw_meta[pid] = (exp, bun,
                             conheader.imported_packages(hdr, info, j))
        grafts = stockgraft.plan(packages, raw_meta, {old_mesh_pid}, say_once)

        safe = safe_id(outfit["name"], used_names, f"Outfit{k + 1}")
        mesh_new = f"/{plugin}/Outfits/{safe}"
        renames = {mesh_name.lower(): mesh_new}
        for pkg in packages.values():
            low = pkg["name"].lower()
            if low in renames or low in external \
                    or low.startswith(f"/{plugin.lower()}/"):
                continue
            tail = pkg["name"][6:] if low.startswith("/game/") \
                else pkg["name"].lstrip("/")
            if low in diff_owner and diff_owner[low] != k:
                renames[low] = f"/{plugin}/{safe}/{tail}"
            else:
                renames[low] = f"/{plugin}/{tail}"
        extra_imports = {}
        for g in grafts.values():
            renames[g["name"].lower()] = f"/{plugin}/{g['name'][6:]}"
        for g in grafts.values():
            new_name = renames[g["name"].lower()]
            gp = ZenPackage(g["data"])
            for e in gp.exports:
                if e["gimp"] == cityhash.NULL_INDEX:
                    continue
                extra_imports[e["gimp"]] = cityhash.object_id(
                    new_name, pkgedit.export_object_path(gp, e))
        new_data, new_ids = rename.rewrite_chunks(
            toc, packages, renames, fix_arcs=True,
            extra_imports=extra_imports)

        pid_map = {pid: cityhash.package_id(renames[pkg["name"].lower()])
                   for pid, pkg in packages.items()
                   if pkg["name"].lower() in renames}
        for gpid, g in grafts.items():
            pid_map[gpid] = cityhash.package_id(renames[g["name"].lower()])
        entry_meta = {pid: (exp, bun, [pid_map.get(p, p) for p in deps])
                      for pid, (exp, bun, deps) in raw_meta.items()}

        bulks = {}
        for i in range(toc.n):
            if toc.chunk_ids[i][11] in (3, 4):
                cid12 = new_ids.get(i, toc.chunk_ids[i])
                d = new_data.get(i)
                # Unchanged bulk data rides as a (id, toc, index) reference so
                # its compressed blocks can be copied across untouched.
                bulks.setdefault(
                    int.from_bytes(cid12[:8], "little"), []).append(
                        (cid12, d) if d is not None else (cid12, toc, i))

        for pid, pkg in packages.items():
            if pkg["name"].lower() in external:
                continue                # a ~mods pak serves it, not us
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

        if grafts:
            graft_data = stockgraft.rewrite(grafts, packages, renames,
                                            extra_imports)
            for gpid, g in grafts.items():
                new_pid = pid_map[gpid]
                if new_pid in merged:
                    continue            # another outfit already carried it
                merged[new_pid] = dict(
                    name=renames[g["name"].lower()], data=graft_data[gpid],
                    deps=[pid_map.get(p, p) for p in g["deps"]],
                    exp=g["exp"], bun=g["bun"],
                    bulks=[(new_pid.to_bytes(8, "little") + bytes(bid[8:]),
                            bdata) for bid, bdata in g["bulks"]])

        obj = mesh_object_name(toc, packages[old_mesh_pid]["chunk"])
        entries_outfits.append((outfit, f"{mesh_new}.{obj}", player))
        wearers.append(dict(toc=toc, packages=packages, renames=renames,
                            mesh_chunk=packages[old_mesh_pid]["chunk"],
                            mesh_package=mesh_new, mesh_object=obj,
                            safe=safe, player=player, outfit=outfit))

    # ---- variants from the modular standard's optional paks --------------
    # An entry is (label, [utoc, ...]): one pak is a plain toggle, several
    # are a user-composed combination applied as one row.
    entries_toggles = []
    entries_weapons = []
    opened = {}
    for label, utocs in extras:
        tick(f"reading part: {label}")
        parts, parts_index = [], {}
        weapon_parts = []
        for utoc in utocs:
            key = os.path.normcase(os.path.abspath(utoc))
            if key not in opened:
                etoc = iostore.Toc(utoc)
                tocs.append(etoc)
                epkgs = rename.read_packages(etoc)
                # Indexing decompresses the whole pak (a recolor can be a
                # 100 MB texture); once per pak, not once per outfit.
                opened[key] = (etoc, epkgs,
                               toggles.export_index(etoc, epkgs))
            etoc, epkgs, eindex = opened[key]
            if weapons.is_weapon_pak(epkgs):
                # A weapon recolour cannot ride an outfit tile -- the outfit
                # never references the weapon -- but it CAN become a tile in
                # Dresscode's WEAPONS menu.
                weapon_parts.append((etoc, epkgs))
                continue
            parts.append((etoc, epkgs))
            parts_index.update(eindex)
        if weapon_parts and weapon_tiles:
            wsafe = safe_id(f"{label}", used_names,
                            f"Weapon{len(used_names)}")
            got = weapons.build_tile(plugin, wsafe, weapon_parts, say,
                                     label=label)
            if got is None:
                say(f"      note: {label} changes a weapon, which takes "
                    "copying the weapon from the game's own files -- not "
                    "found here. Keep that pak in ~mods instead: it works "
                    "there alongside the Dresscode outfit.")
            else:
                carried, rows = got
                for pid, rec in carried.items():
                    merged.setdefault(pid, rec)
                for row_label, mesh_path, player, stock_name in rows:
                    entries_weapons.append(dict(
                        label=row_label, mesh=mesh_path, player=player,
                        stock=stock_name))
        if not parts:
            continue
        for w in wearers:
            slots, _overridden, retex = toggles.plan(
                w["toc"], w["packages"], w["mesh_chunk"], parts,
                refs=w.setdefault(
                    "refs", toggles.references(w["toc"], w["packages"])))
            if not slots:
                names = [p["name"] for _et, ep in parts for p in ep.values()]
                if names and all(n.lower().startswith("/game/")
                                 for n in names):
                    say(f"      note: {label} only changes game files "
                        f"{w['outfit']['name']} never uses (a weapon, say) "
                        "-- no tile made. Keep that pak in ~mods instead: "
                        "it works there alongside the Dresscode outfit.")
                else:
                    say(f"      note: {label} changes nothing on "
                        f"{w['outfit']['name']} -- skipped")
                continue
            safe = safe_id(f"{label}", used_names, f"Extra{len(used_names)}")
            made, actor = toggles.emit(
                w["toc"], w["packages"], parts, slots,
                w["renames"], plugin, w["safe"], safe, w["mesh_package"],
                w["mesh_object"],
                index=w.setdefault("exports", toggles.export_index(
                    w["toc"], w["packages"])),
                parts_index=parts_index, retex=retex)
            for item in made:
                pid = cityhash.package_id(item["name"])
                if pid in merged:
                    continue
                # The header's export count is what the loader sizes its
                # export array from -- a blueprint has eight, not one.
                merged[pid] = dict(
                    name=item["name"], data=item["data"], deps=item["deps"],
                    exp=len(ZenPackage(item["data"]).exports), bun=1,
                    bulks=item["bulks"])
            entries_toggles.append((w, label, actor, len(slots)))
            say(f"      toggle  {w['outfit']['name']}: {label}   "
                f"({len(slots)} slot{'s' if len(slots) != 1 else ''})")

    # ---- synthesized packages ------------------------------------------
    def add(name, data, deps):
        pid = cityhash.package_id(name)
        merged[pid] = dict(name=name, data=data, deps=deps,
                           exp=len(ZenPackage(data).exports), bun=1, bulks=[])
        return pid

    # Real mods register their content in AssetRegistry.bin -- toggle
    # blueprints with the full Blueprint tag set (and packages flagged
    # 0x40000), packs as EndMaterialPack, meshes as SkeletalMesh. Ours
    # registered only previews and metadata, and its toggle actors never
    # loaded in game while a real mod's did.
    registry_assets = []
    for w, label, actor, _n in entries_toggles:
        bp_pkg, bp_obj = actor.rsplit(".", 1)
        folder = bp_pkg.rsplit("/", 1)[0]
        registry_assets.append(dict(
            object_path=actor, package_path=folder, class_name="Blueprint",
            package_name=bp_pkg, asset_name=bp_obj, flags=0x40000,
            tags=[
                ("GeneratedClass",
                 f"BlueprintGeneratedClass'{bp_pkg}.{bp_obj}_C'"),
                ("ParentClass", "Class'/Script/EndGame.EndPlayerCharacter'"),
                ("NativeParentClass",
                 "Class'/Script/EndGame.EndPlayerCharacter'"),
                ("ClassFlags", "12847124"),
                ("BlueprintType", "BPTYPE_Normal"),
                ("IsDataOnly", "True"),
                ("NumReplicatedProperties", "0"),
                ("NativeComponents", "4"),
                ("BlueprintComponents", "0"),
                ("BlueprintPath", bp_obj),
            ]))
        pack_pkg = f"{bp_pkg}_MP"
        registry_assets.append(dict(
            object_path=f"{pack_pkg}.{bp_obj}_MP", package_path=folder,
            class_name="EndMaterialPack", package_name=pack_pkg,
            asset_name=f"{bp_obj}_MP", tags=[]))
    for _outfit, mesh_path, _player in entries_outfits:
        mesh_pkg, mesh_obj = mesh_path.rsplit(".", 1)
        registry_assets.append(dict(
            object_path=mesh_path,
            package_path=mesh_pkg.rsplit("/", 1)[0],
            class_name="SkeletalMesh", package_name=mesh_pkg,
            asset_name=mesh_obj, tags=[]))
    for e in entries_weapons:
        mesh_pkg, mesh_obj = e["mesh"].rsplit(".", 1)
        registry_assets.append(dict(
            object_path=e["mesh"],
            package_path=mesh_pkg.rsplit("/", 1)[0],
            class_name="SkeletalMesh", package_name=mesh_pkg,
            asset_name=mesh_obj, tags=[]))
    previews = []
    for k, (outfit, _mesh, _player) in enumerate(entries_outfits):
        if not outfit.get("preview"):
            previews.append(TEMPLATE_ICON)
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

    def toggle_body(w, label, actor):
        """A toggle row: no mesh of its own, just the actor that applies a
        material pack to the outfit already worn."""
        # Alone with its outfit (the split builds one mod per outfit), a
        # toggle needs no outfit prefix on its menu row.
        row_name = label if len(entries_outfits) == 1 \
            else f"{w['outfit']['name']} - {label}"
        return [
            (F["GeneralData"], ("struct", "UDS_ModData_General", GUID_GENERAL, [
                (F["Name"], ("str", row_name)),
                # The menu shows the DESCRIPTION under a tile, not the name
                # -- real mods put their row labels there.
                (F["OutfitDescription"], ("str", row_name)),
                (F["PreviewImage"], ("softpath", TEMPLATE_ICON)),
            ])),
            (F["SkeletalMeshData"],
             ("struct", "UDS_AssetType_SkeletalMesh", GUID_SKM, [
                 (F["PlayerType"],
                  ("enum", "EPlayerType", f"EPlayerType::{w['player']}")),
                 (F["SkeletalMesh"], ("softpath", None)),
                 (F["Actor"], ("softpath", actor)),
             ])),
            (F["AdditionalData"], ("struct", "UDS_AssetType_Custom",
                                   GUID_CUSTOM, [
                (F["DataAssets"], ("map", "NameProperty", "ObjectProperty")),
                (F["CustomData"], ("map", "NameProperty", "StructProperty")),
            ])),
        ]

    bodies = []
    for k, (outfit, mesh_path, player) in enumerate(entries_outfits):
        bodies.append([
            (F["GeneralData"], ("struct", "UDS_ModData_General", GUID_GENERAL, [
                (F["Name"], ("str", outfit["name"])),
                (F["OutfitDescription"],
                 ("str", outfit["description"] or outfit["name"])),
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
        # Its own toggles follow it: every mod observed groups them that
        # way, and a toggle names no outfit, so its position is the only
        # thing tying it to one.
        for w, label, actor, _n in entries_toggles:
            if w["outfit"] is outfit:
                bodies.append(toggle_body(w, label, actor))

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

    if entries_weapons:
        # A second data asset of the SAME class, told apart by "Mod Type" --
        # exactly how Dresscode's own container splits its stock costume and
        # stock weapon lists between two assets.
        wbodies = []
        for e in entries_weapons:
            preview = weapons.stock_preview(e["stock"]) or TEMPLATE_ICON
            wbodies.append([
                (F["GeneralData"],
                 ("struct", "UDS_ModData_General", GUID_GENERAL, [
                     (F["Name"], ("str", e["label"])),
                     (F["OutfitDescription"], ("str", e["label"])),
                     (F["PreviewImage"], ("softpath", preview)),
                 ])),
                (F["SkeletalMeshData"],
                 ("struct", "UDS_AssetType_SkeletalMesh", GUID_SKM, [
                     (F["PlayerType"],
                      ("enum", "EPlayerType", f"EPlayerType::{e['player']}")),
                     (F["SkeletalMesh"], ("softpath", e["mesh"])),
                     (F["Actor"], ("softpath", None)),
                 ])),
                (F["AdditionalData"], ("struct", "UDS_AssetType_Custom",
                                       GUID_CUSTOM, [
                    (F["DataAssets"], ("map", "NameProperty",
                                       "ObjectProperty")),
                    (F["CustomData"], ("map", "NameProperty",
                                       "StructProperty")),
                ])),
            ])
        weap_props = [
            ("Character Data",
             ("array_structs", "UDS_ModData_Character", GUID_CHAR, wbodies)),
            ("Mod Type", ("byte_enum", "E_ModType", weapons.MOD_TYPE_WEAPON)),
            ("NativeClass", ("obj", -1)),
        ]
        weap_name = f"/{plugin}/MetaData/WeaponData"
        add(weap_name, mkpkg.build(
            weap_name, "WeaponData", CHAR_PKG, "PDA_ModData_Character_C",
            weap_props,
            imports=[cityhash.object_id(CHAR_PKG,
                                        "PDA_ModData_Character_C"),
                     NULL,
                     cityhash.object_id(CHAR_PKG,
                                        "Default__PDA_ModData_Character_C")],
            graph=[(cityhash.package_id(CHAR_PKG), [(0, 0)])]),
            [cityhash.package_id(CHAR_PKG)])
        registry_assets.append(dict(
            object_path=f"{weap_name}.WeaponData",
            package_path=f"/{plugin}/MetaData",
            class_name="PDA_ModData_Character_C", package_name=weap_name,
            asset_name="WeaponData",
            tags=data_asset_tags("PDA_ModData_Character_C", CHAR_PKG,
                                 "WeaponData")))

    # Now that the whole container is known, every reference leaving it has
    # to say so -- see mark_external_arcs.
    shipped = set(merged)
    for rec in merged.values():
        patched = mark_external_arcs(rec["data"], shipped)
        if patched is not None:
            rec["data"] = patched

    # ---- container header chunk ----------------------------------------
    order = sorted(merged)
    hdr = struct.pack("<QIIIIQ", cid, len(order), 0, 0, 8, 0xC1640000)
    hdr += struct.pack("<I", len(order))
    hdr += struct.pack(f"<{len(order)}Q", *order)
    store = bytearray()
    for j, pid in enumerate(order):
        rec = merged[pid]
        # LoadOrder is any dense permutation; the field after it is 0xFFFFFFFF
        # in every donor mod AND every CE-cooked pak -- never 0.
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

    n_files = 1 + len(merged) + sum(len(r["bulks"]) for r in merged.values())
    packed = [0]

    def blocks_of(payload):
        packed[0] += 1
        tick(f"packing file {packed[0]}/{n_files}")
        return rename.pack_blocks(payload, template_toc.block_size, comp)

    # Bulk data referenced as (id, toc, index) is copied across still
    # compressed, checksum row included -- skipping the decompress/re-Oodle
    # round trip that dominates on a texture-heavy mod. Block method ids are
    # per-container, so remap them to the template's table; anything that
    # does not map falls back to recompressing.
    _methods = {}

    def ported_blocks(btoc, bi):
        key = id(btoc)
        if key not in _methods:
            _methods[key] = [
                next((j for j, n in enumerate(template_toc.methods)
                      if n.lower() == m.lower()), None)
                for m in btoc.methods]
        mm = _methods[key]
        out = []
        for data, usize, method in original_blocks(btoc, bi):
            m2 = mm[method] if method < len(mm) else None
            if m2 is None:
                return None
            out.append((data, usize, m2))
        # An all-raw chunk means the cooker never compressed it -- copying
        # it would carry the bloat along, so let it go through the
        # compressor. Raw blocks WITHIN a compressed chunk stay: those are
        # raw because they did not shrink.
        if out and all(m == 0 for _d, _u, m in out):
            return None
        return out

    def meta_row(payload):
        return hashlib.sha1(payload).digest() + b"\0" * 12 + b"\x01"

    # Packages first, bulk data after -- every container observed, the
    # game's own included, keeps the two segregated.
    chunks = [dict(id=cid.to_bytes(8, "little") + b"\0\0\0\x0a",
                   blocks=blocks_of(hdr), size=len(hdr))]
    metas = [meta_row(hdr)]
    paths = []
    prefix = f"/{plugin}/"
    rels = {}
    for pid in order:
        rec = merged[pid]
        cid12 = pid.to_bytes(8, "little") + b"\0\0\0\x02"
        chunks.append(dict(id=cid12, blocks=blocks_of(rec["data"]),
                           size=len(rec["data"])))
        metas.append(meta_row(rec["data"]))
        # Every package was renamed under /<plugin>/ (or already lived
        # there), so stripping the fixed-length prefix is always right.
        rels[pid] = rec["name"][len(prefix):]
        paths.append((rels[pid] + ".uasset", len(chunks) - 1))
    for pid in order:
        for entry in merged[pid]["bulks"]:
            if len(entry) == 3:
                bid, btoc, bi = entry
                blocks = ported_blocks(btoc, bi)
                if blocks is None:
                    bdata = btoc.read(bi)
                    blocks, size = blocks_of(bdata), len(bdata)
                    metas.append(meta_row(bdata))
                else:
                    packed[0] += 1
                    tick(f"packing file {packed[0]}/{n_files}")
                    size = btoc.offlen[bi][1]
                    metas.append(bytes(
                        btoc.d[btoc.meta_off + bi * 33:
                               btoc.meta_off + (bi + 1) * 33]))
            else:
                bid, bdata = entry
                blocks, size = blocks_of(bdata), len(bdata)
                metas.append(meta_row(bdata))
            chunks.append(dict(id=bytes(bid), blocks=blocks, size=size))
            ext = ".uptnl" if bid[11] == 4 else ".ubulk"
            paths.append((rels[pid] + ext, len(chunks) - 1))

    directory = dirindex.build_dir_index(mount, paths)
    tick("writing the mod file")
    body, ucas, _offlen, block_table = writer.build_container(
        template_toc, chunks, template_toc.block_size)
    head = bytearray(writer.build_toc_header(
        template_toc, len(chunks), len(block_table), len(directory),
        template_toc.block_size))
    struct.pack_into("<Q", head, 0x38, cid)
    metas = b"".join(metas)

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
