"""
convert.py -- converts a FFVII Rebirth costume mod between its two formats.

    python convert.py "D:\\mods\\SomeMod"        a Dresscode mod folder
    python convert.py "D:\\mods\\SomeMod_P.utoc" a loose pak mod

Point it at a mod -- or drop one on convert.py: a folder, several at once, a
.zip/.7z/.rar, or a folder of archives; archives nested inside archives are
unpacked too. It works out which format each mod is in, says what it will
produce, asks once, and writes every conversion beside its original (beside
the archive, for a mod that arrived in one). Originals are never touched.

THE TWO FORMATS
---------------
DRESSCODE   A plugin folder: <Mod>.uplugin and a container under
            Content/Paks/WindowsNoEditor, whose packages are named /<Mod>/...
            The outfit is ADDED to the Dresscode menu and picked there.

LOOSE PAK   A .utoc/.ucas/.pak dropped in ~mods, whose packages are named
            /Game/... The outfit REPLACES a stock one and is always worn.

So a conversion renames the MESH package onto the stock costume it replaces
and moves every other package into that costume's folder under /Game/, plus a
new .pak carrying the mount point the new format expects. lib/rename.py does
the renames, lib/pakfile.py the pak.

The /Game/ move is NOT cosmetic. Packages are found by ID, but the async
loader silently skips imports into a root that is not mounted -- and a ~mods
container mounts no plugin root. Probed in game: a mesh whose materials kept
their /<Mod>/... names loads and renders, with every such material slot NULL
(the default checker) while its /Game/ slots resolve fine.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
This is not patch.py. There is no game detection, no mod library, no backups, no
in-place editing, no archive handling -- a converter that writes a new mod next
to the old one needs none of it, and every one of those is a way to damage
something. Conversion only ever creates.
"""

import base64
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import conheader                                                # noqa: E402
import drops                                                    # noqa: E402
import iostore                                                  # noqa: E402
import mkdc                                                     # noqa: E402
import moddata                                                  # noqa: E402
import pakfile                                                  # noqa: E402
import pngfile                                                  # noqa: E402
import rename                                                   # noqa: E402
import texread                                                  # noqa: E402
import zen                                                      # noqa: E402

# Diagnostic switch (--keep-registration): convert without dropping the two
# Dresscode registration assets, so the container header is remapped in place
# instead of rebuilt. Isolates the drop path when bisecting a broken output.
KEEP_REGISTRATION = False

# The CONTAINER mounts at the player-character folder, exactly like every
# loose pak mod confirmed working in game -- root mounts exist in the wild but
# none has been verified, so we do not pioneer one. Every converted package
# lands under this folder. The .pak is mounted separately and shallowly; see
# pakfile.LOOSE_MOUNT, which is not this.
CONTAINER_MOUNT = "../../../End/Content/Character/Player/"
LOOSE_ROOT = "/Game/Character/Player/"


def find_container(path):
    """The mod's .utoc, given a folder or any of its three files."""
    if os.path.isfile(path):
        stem = os.path.splitext(path)[0]
        return stem + ".utoc" if os.path.exists(stem + ".utoc") else None
    for root, _dirs, files in os.walk(path):
        for f in files:
            if f.lower().endswith(".utoc"):
                return os.path.join(root, f)
    return None


def find_uplugin(path):
    """The mod's .uplugin, which is what makes it a plugin at all."""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    for _ in range(4):                          # the container sits 3 deep
        try:
            found = [f for f in os.listdir(folder) if f.lower().endswith(".uplugin")]
        except OSError:
            return None
        if found:
            return os.path.join(folder, found[0])
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent
    return None


def find_mods(path):
    """
    Every mod under `path`, as [(utoc, uplugin or None)] -- one entry per mod.

    A .uplugin is the deciding evidence for Dresscode -- it is what the loader
    reads and what a loose pak has no equivalent of. A dropped folder can hold
    several mods; each .uplugin is one, and a .utoc with no .uplugin above it
    is a loose pak.
    """
    if os.path.isfile(path):
        utoc = find_container(path)
        return [(utoc, find_uplugin(utoc))] if utoc else []
    found, seen = [], set()
    for root, _dirs, files in os.walk(path):
        for f in sorted(files):
            if not f.lower().endswith(".utoc"):
                continue
            utoc = os.path.join(root, f)
            uplugin = find_uplugin(utoc)
            if uplugin:
                if uplugin.lower() in seen:     # one container per plugin
                    continue
                seen.add(uplugin.lower())
            found.append((utoc, uplugin))
    return found


def plan_variants(toc, plugin):
    """
    A conversion plan per wearable outfit -- a Dresscode mod can register
    several variants, and each becomes its own loose pak.

    Each variant's mesh takes over the character's DEFAULT costume package, so
    it is worn without a menu. Everything else moves into a subfolder of that
    same costume folder, named for the mod: /<Mod>/... names would keep their
    IDs, but the loader silently skips imports into an unmounted root, so they
    must live under /Game/ to resolve from ~mods.

    A variant carries everything except what provably is not its: the
    registration assets, preview images no mesh uses, and packages reachable
    only from OTHER variants' meshes. Packages no variant references (physics
    assets and the like, wired up in ways a dependency walk cannot see) ride
    along in every variant -- a few wasted megabytes beat a missing feature.

    Returns [(outfit, target, renames, objects, drop)], one entry per variant.
    """
    assets = moddata.find_data_assets(toc)
    if "character" not in assets:
        raise RuntimeError("no Dresscode outfit data in this mod")
    outfits = moddata.read_outfits(toc.read(assets["character"]))
    wearable = [o for o in outfits
                if o["skeletal_mesh"] and not o["skeletal_mesh"].startswith("/Game/")]
    if not wearable:
        raise RuntimeError("this mod registers no mesh of its own to convert")

    # The registration assets are Dresscode's, and only Dresscode's. Carried
    # into a loose pak they become data assets whose CLASS lives in a plugin
    # that is not mounted when ~mods is -- and the loader still enumerates them
    # at startup. They describe a mod that, in this format, does not exist.
    registration = set() if KEEP_REGISTRATION else set(assets.values())

    packages = rename.read_packages(toc)
    own = {p["name"].lower() for p in packages.values()}
    by_name = {p["name"].lower(): pid for pid, p in packages.items()}
    deps = header_deps(toc)

    def closure(seed_pid):
        """Everything a mesh pulls in, walking the container header's
        dependency lists -- in-container packages only."""
        keep, todo = set(), [seed_pid]
        while todo:
            pid = todo.pop()
            if pid in keep or pid not in packages:
                continue
            keep.add(pid)
            todo += deps.get(pid, [])
        return keep

    closures = []
    for outfit in wearable:
        mesh = outfit["skeletal_mesh"].split(".")[0]
        pid = by_name.get(mesh.lower())
        closures.append(closure(pid) if pid is not None else set())

    # Preview images exist for the Dresscode menu; the game never asks for
    # them from a loose pak. Each rides along in ITS outfit's variant anyway,
    # because converting back to Dresscode then restores the original cooked
    # texture untouched -- that is what makes the round trip lossless. Only a
    # preview no wearable outfit references is dropped outright.
    preview_users = {}                  # pid -> indexes of outfits using it
    for j, o in enumerate(wearable):
        if o["preview_image"]:
            p = by_name.get(o["preview_image"].split(".")[0].lower())
            if p is not None and not any(p in c for c in closures):
                preview_users.setdefault(p, set()).add(j)
    orphan_previews = set()
    for o in outfits:
        if o in wearable or not o["preview_image"]:
            continue
        p = by_name.get(o["preview_image"].split(".")[0].lower())
        if p is not None and p not in preview_users \
                and not any(p in c for c in closures):
            orphan_previews.add(p)

    plans = []
    for k, outfit in enumerate(wearable):
        mesh = outfit["skeletal_mesh"].split(".")[0]
        if by_name.get(mesh.lower()) is None:
            print(f"  skipping {outfit['name'] or mesh!r}: its mesh package "
                  "is not in the container")
            continue
        target = moddata.default_costume_package(outfit["player_type"])
        if not target:
            print(f"  skipping {outfit['name'] or mesh!r}: unknown character "
                  f"{outfit['player_type']!r}")
            continue

        others = set().union(*(c for j, c in enumerate(closures) if j != k)) \
            if len(closures) > 1 else set()
        not_mine = {p for p, users in preview_users.items() if k not in users}
        exclusive_elsewhere = (others - closures[k]) | not_mine \
            | orphan_previews

        # ".../PC0002_00_Tifa_Standard" -- the costume folder the mesh lives in.
        costume_root = target.rsplit("/Model/", 1)[0]

        renames, objects, drop = {}, {}, set()
        for pid, info in packages.items():
            name = info["name"]
            if info["chunk"] in registration or pid in exclusive_elsewhere:
                drop.add(name)
            if name.lower() == mesh.lower():
                renames[name.lower()] = target
                # The mesh's EndCharacterConditionUserData soft-references a
                # condition (petrify) mesh, and Dresscode authors can leave it
                # pointing into their own uncooked project folder -- Dresscode
                # never follows the reference, but the stock costume pipeline
                # does. Point it at the condition mesh of the costume being
                # replaced.
                for cond in condition_refs(toc, info["chunk"], own):
                    if cond.lower() != f"{target}_Condition".lower():
                        renames[cond.lower()] = f"{target}_Condition"
                # The object inside has to take the stock name too. The game
                # imports /Game/.../PC0002_00.PC0002_00, and an import ID
                # hashes the object name with the package's -- so a mesh still
                # called MyOutfit answers to an ID nothing asks for, and the
                # override silently does nothing.
                stock_object = target.rsplit("/", 1)[-1]
                objects[name.lower()] = {e: stock_object for e in info["exports"]
                                         if "/" not in e and e != stock_object
                                         and e == mesh.rsplit("/", 1)[-1]}
            elif name.lower().startswith(f"/{plugin.lower()}/"):
                renames[name.lower()] = \
                    f"{costume_root}/{plugin}{name[len(plugin) + 1:]}"
            else:
                renames[name.lower()] = name
        plans.append((outfit, target, renames, objects, drop))
    if not plans:
        raise RuntimeError("none of this mod's outfits can be converted")
    # The survey rides along so the round-trip recorder does not pay for a
    # second full read of every package.
    return plans, dict(packages=packages, assets=assets, outfits=outfits)


def header_deps(toc):
    """{package ID -> imported package IDs} from the container header."""
    for i in range(toc.n):
        if toc.chunk_ids[i][11] != 10:
            continue
        hdr = toc.read(i)
        info = conheader.parse(hdr)
        if not info:
            break
        return {pid: conheader.imported_packages(hdr, info, k)
                for k, pid in enumerate(conheader.package_ids(hdr, info))}
    return {}


def condition_refs(toc, chunk, own):
    """Condition-mesh packages the mesh soft-references outside this mod."""
    out = set()
    for s in zen.ZenPackage(toc.read(chunk)).names:
        pkg = s.split(".")[0]
        if (s.startswith("/") and pkg.lower().endswith("_condition")
                and pkg.lower() not in own):
            out.add(pkg)
    return out


def loose_path(package_name):
    """Container-relative path, under the player-character mount."""
    if not package_name.startswith(LOOSE_ROOT):
        raise RuntimeError(f"cannot place {package_name} in a loose pak")
    return package_name[len(LOOSE_ROOT):]


# ---------------------------------------------------------------------------
# Loose pak -> Dresscode: the template.
#
# A Dresscode mod needs things a loose pak simply does not have -- a display
# name, an author, per-outfit names, optional preview images -- and prompting
# for each in a console is miserable. So the first drop of a loose mod writes
# a dresscode.json template beside its paks, prefilled with everything
# detectable, and the second drop reads it and builds. Editing it is optional:
# the prefilled values already work.
#
# The folder layout IS the variant structure: paks directly in the dropped
# folder are a single outfit; each sub-folder holding paks is one variant,
# named for its folder.
# ---------------------------------------------------------------------------

TEMPLATE = "dresscode.json"
IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def loose_layout(source, mods):
    """
    (mod_name, [(relative folder, utoc)]) when `source` is the root of a loose
    pak mod in the shape the template supports -- otherwise None.
    """
    if not mods or any(uplugin for _utoc, uplugin in mods):
        return None
    root = os.path.normcase(os.path.abspath(source))
    parts = []
    for utoc, _ in mods:
        d = os.path.dirname(os.path.abspath(utoc))
        if os.path.normcase(d) == root:
            parts.append((".", utoc))
        elif os.path.normcase(os.path.dirname(d)) == root:
            parts.append((os.path.basename(d), utoc))
        else:
            return None                 # deeper nesting; not a shape we know
    kinds = {p == "." for p, _ in parts}
    if kinds == {True} and len(parts) > 1:
        return None                     # several paks loose in one folder
    if len(kinds) > 1:
        return None                     # a pak both at the root and in subs
    name = os.path.basename(os.path.abspath(source).rstrip("\\/"))
    if name.lower().endswith(" (loose pak)"):
        name = name[:-len(" (loose pak)")]
    return name, sorted(parts)


def images_in(folder):
    try:
        return sorted(f for f in os.listdir(folder)
                      if f.lower().endswith(IMAGE_EXTS))
    except OSError:
        return []


def find_image(folder, preferred):
    """
    The picture a folder nominates, with no configuration: a file named
    `preferred` (preview.png, icon.jpg, ...) wins, otherwise the first image
    alphabetically. Nobody should ever have to type a file path into JSON --
    where a picture SITS is the whole interface.
    """
    pics = images_in(folder)
    for p in pics:
        if os.path.splitext(p)[0].lower() == preferred:
            return os.path.join(folder, p)
    return os.path.join(folder, pics[0]) if pics else None


def write_template(source, mod_name, parts, prefill=None, restore=None):
    """Prefill dresscode.json with the little a build needs. Pictures are on
    purpose NOT in here -- they are picked up by where they sit.

    `prefill` carries real values when the loose mod was itself converted
    from a Dresscode mod; `restore` is that conversion's opaque record of the
    original, which makes converting back exact."""
    prefill = prefill or {}
    pre_outfits = prefill.get("outfits", {})
    outfits = [{"folder": rel,
                "name": pre_outfits.get(rel, (None,))[0]
                or (mod_name if rel == "." else rel),
                "description": pre_outfits.get(rel, (None, ""))[1]}
               for rel, _utoc in parts]
    template = {
        "_how_this_works": [
            "convert.py made this file. Drop the folder on it again and it",
            "builds the Dresscode mod -- changing anything below is optional.",
            "",
            "What you can change:",
            "  \"name\" at the top       what the whole mod is called",
            "  \"name\" in an outfit     what THAT outfit is called in",
            "                          Dresscode's outfit menu",
            "  \"author\", descriptions  shown on the mod's page",
            "",
            "Pictures (optional). Two different pictures exist:",
            "  the THUMBNAIL, one per mod, shown in Dresscode's mod list",
            "     -> put a picture next to this file",
            "  the PREVIEW, one per outfit, shown when picking outfits",
            "     -> put a picture inside that outfit's folder",
            "  If a folder has several pictures, name the right one",
            "  preview.png (or icon.png for the thumbnail).",
            "",
            "Do NOT change the \"folder\" lines -- they say where each",
            "outfit's files live.",
        ],
        "name": prefill.get("name") or mod_name,
        "author": prefill.get("author", ""),
        "description": prefill.get("description", ""),
        "category": prefill.get("category") or "Outfit",
        "version": prefill.get("version") or "1.0.0",
        "outfits": outfits,
    }
    if restore:
        template["_how_this_works"] += [
            "",
            "\"restore\" below is the original Dresscode mod, recorded so",
            "that converting back reproduces it exactly. Leave it alone.",
        ]
        template["restore"] = restore
    path = os.path.join(source, TEMPLATE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    return path


def read_template(source, parts):
    """Parse and validate dresscode.json against the folder layout. Returns
    (meta, outfits) with image paths resolved, or raises with what to fix."""
    path = os.path.join(source, TEMPLATE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as ex:
        raise RuntimeError(f"{TEMPLATE} is not valid JSON: {ex}")

    if not str(data.get("name", "")).strip():
        raise RuntimeError(f'{TEMPLATE} needs at least a "name"')

    by_folder = {rel: utoc for rel, utoc in parts}
    outfits = []
    for entry in data.get("outfits") or []:
        rel = str(entry.get("folder", ""))
        if rel not in by_folder:
            raise RuntimeError(
                f'{TEMPLATE} mentions an outfit folder that is not there: '
                f'"{rel}". The folders are: {", ".join(sorted(by_folder))}. '
                f'(The "folder" lines must match the real folders.)')
        # Pictures come from where they sit, never from typed paths.
        folder = source if rel == "." else os.path.join(source, rel)
        outfits.append(dict(
            folder=rel, utoc=by_folder[rel],
            name=str(entry.get("name", "")) or (rel if rel != "." else
                                                str(data["name"])),
            description=str(entry.get("description", "")),
            preview=find_image(folder, "preview"),
        ))
    missing = sorted(set(by_folder) - {o["folder"] for o in outfits})
    if missing:
        raise RuntimeError(
            f"{TEMPLATE} is missing its entry for: {', '.join(missing)}. "
            "Delete the file and drop the folder again to get a fresh one.")
    if not outfits:
        raise RuntimeError(
            f"{TEMPLATE} has no outfits in it. Delete the file and drop the "
            "folder again to get a fresh one.")

    meta = dict(
        name=str(data["name"]).strip(),
        author=str(data.get("author", "")),
        description=str(data.get("description", "")),
        category=str(data.get("category", "")) or "Outfit",
        version=str(data.get("version", "")) or "1.0.0",
        icon=find_image(source, "icon"),
        restore=data.get("restore"),
    )
    return meta, outfits


def plugin_id(name):
    """
    The display name reduced to a plugin identifier.

    Dresscode looks a mod up by its folder name and ignores it unless that
    exactly equals the .uplugin inside (the patcher repairs mismatched
    downloads for the same reason). Building from one derived id -- folder,
    .uplugin and container all -- makes a mismatch impossible.
    """
    cleaned = "".join(c for c in name if c.isalnum() or c == "_")
    return cleaned or "Mod"


# ---------------------------------------------------------------------------
# The round-trip record. Converting Dresscode -> loose throws real things
# away -- the registration assets, the registry, the pak's exact shape, every
# original package name. All of it is small except the packages, and THOSE
# survive as the loose paks themselves. So the conversion stores the rest,
# compressed, inside the dresscode.json it generates; converting back reads
# it and reproduces the original mod instead of synthesizing a lookalike.
# Deleting the key (or editing the visible fields) simply falls back to a
# fresh build.
# ---------------------------------------------------------------------------

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


def restore_matches(rt, meta, outfits):
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
        if md5_of(o.get("preview")) != by_folder.get(o["folder"]):
            return False
    return md5_of(meta.get("icon")) == rt.get("icon_md5")


def record_roundtrip(toc, uplugin, plugin, plans, ctx, mod_out, layout):
    """
    Leave everything beside the written loose paks that converting BACK will
    need: a prefilled dresscode.json, each outfit's preview as a PNG a person
    can see and swap, the plugin icon -- and the opaque restore record.

    `layout` is [(rel_folder, plan)], "." meaning the mod's root folder.
    """
    packages = ctx["packages"]
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
        got = texread.extract(toc.read(chunk))
        if not got:
            return
        w, h, bgra = got
        data = pngfile.encode(w, h, bgra)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, stem + ".png")
        with open(path, "wb") as f:
            f.write(data)
        rel = os.path.relpath(path, mod_out).replace("\\", "/")
        pngs[rel] = hashlib.md5(data).hexdigest()

    for rel, (outfit, *_rest) in layout:
        ref = outfit.get("preview_image")
        chunk = None
        if ref:
            pid = by_name.get(ref.split(".")[0].lower())
            chunk = packages[pid]["chunk"] if pid in packages else None
        if chunk is not None:
            put_png(mod_out if rel == "." else os.path.join(mod_out, rel),
                    "preview", chunk)

    icon_md5 = None
    icon_file = os.path.join(os.path.dirname(uplugin), "Resources",
                             "Icon128.png")
    if os.path.exists(icon_file):
        with open(icon_file, "rb") as f:
            icon_data = f.read()
        with open(os.path.join(mod_out, "icon.png"), "wb") as f:
            f.write(icon_data)
        icon_md5 = hashlib.md5(icon_data).hexdigest()

    # --- the original container's shape ---
    hdr_chunk = next(i for i in range(toc.n) if toc.chunk_ids[i][11] == 10)
    hdr = toc.read(hdr_chunk)
    info = conheader.parse(hdr)
    ids = conheader.package_ids(hdr, info)
    entry_of = {}
    for j, pid in enumerate(ids):
        _sz, exp, bun, lo, pad = struct.unpack_from(
            "<Qiiii", hdr, info["store_off"] + j * 32)
        entry_of[pid] = dict(exp=exp, bun=bun, lo=lo, pad=pad, order=j)

    name_of = {pid: p["name"] for pid, p in packages.items()}
    chunk_order = []
    for i in range(toc.n):
        t = toc.chunk_ids[i][11]
        pid = int.from_bytes(toc.chunk_ids[i][:8], "little")
        chunk_order.append(["" if t == 10 else name_of.get(pid, ""), t])

    # Which graph arcs are -1 in the ORIGINAL. The loose conversion must
    # flatten them to 0 (the ~mods loader refuses them); converting back
    # puts them back by ordinal.
    neg_arcs = {}
    for pid, p in packages.items():
        data = toc.read(p["chunk"])
        bad = [k for k, pos in enumerate(mkdc.arc_positions(data))
               if struct.unpack_from("<i", data, pos)[0] == -1]
        if bad:
            neg_arcs[p["name"]] = bad

    reg_chunks = {}
    for kind, chunk in ctx["assets"].items():
        pid = int.from_bytes(toc.chunk_ids[chunk][:8], "little")
        e = entry_of.get(pid, {})
        reg_chunks[name_of[pid]] = dict(
            data=base64.b64encode(
                zlib.compress(toc.read(chunk), 9)).decode("ascii"),
            exp=e.get("exp", 1), bun=e.get("bun", 1),
            deps=[str(d) for d in
                  conheader.imported_packages(hdr, info, e["order"])]
            if e else [])

    # --- the original pak, entry by entry, decompressed ---
    pak_path = os.path.splitext(toc.path)[0] + ".pak"
    with open(pak_path, "rb") as f:
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
            for cond in condition_refs(toc, packages[mesh_pid]["chunk"], own):
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

    visible = dict(
        name=md.get("friendly_name") or plugin,
        author=md.get("created_by", ""),
        description=md.get("description", ""),
        category=md.get("category") or "Outfit",
        version=up.get("VersionName") or "1.0.0",
        outfits={rel: [o["name"], o["description"]]
                 for rel, (o, *_r) in layout},
    )

    record = dict(
        plugin=plugin,
        mount=toc.mount,
        cid=str(toc.container_id),
        hdr_len=len(hdr),
        uplugin=base64.b64encode(uplugin_raw).decode("ascii"),
        icon_md5=icon_md5,
        id_order=[name_of.get(pid, "") for pid in ids],
        entries={name_of[pid]: [e["lo"], e["pad"]]
                 for pid, e in entry_of.items() if pid in name_of},
        chunk_order=chunk_order,
        neg_arcs=neg_arcs,
        reg_chunks=reg_chunks,
        pak_mount=pak_mount,
        pak_seed=str(pak_seed),
        pak_files=[[p, base64.b64encode(zlib.compress(b, 9)).decode("ascii")]
                   for p, b in pak_files],
        variants=variants,
        pngs=pngs,
        visible=visible,
    )

    parts = [(rel, None) for rel, _plan in layout]
    write_template(mod_out, visible["name"], parts,
                   prefill=dict(name=visible["name"],
                                author=visible["author"],
                                description=visible["description"],
                                category=visible["category"],
                                version=visible["version"],
                                outfits={rel: tuple(v) for rel, v in
                                         visible["outfits"].items()}),
                   restore=pack_restore(record))
    print("    recorded  dresscode.json + pictures -- converting the folder "
          "back restores this mod exactly")
    return 0


def loose_to_dresscode(source, mods, assume_yes=False):
    """
    The template flow for a dropped loose pak mod. Returns an exit code, or
    None when `source` is not a loose mod root this direction understands.
    """
    layout = loose_layout(source, mods)
    if layout is None:
        return None
    mod_name, parts = layout

    if not os.path.exists(os.path.join(source, TEMPLATE)):
        path = write_template(source, mod_name, parts)
        print()
        print(f"  {mod_name}  (loose pak -> Dresscode)")
        print(f"      created  {os.path.basename(path)}"
              f"  ({len(parts)} outfit{'s' if len(parts) > 1 else ''})")
        print("      Drop the folder again to build. Before that, if you")
        print(f"      want, open {TEMPLATE} to set names and author, and put")
        print("      a picture in an outfit's folder to give it a preview.")
        return 0

    meta, outfits = read_template(source, parts)
    rt = unpack_restore(meta.get("restore"))
    exact = rt is not None and restore_matches(rt, meta, outfits)
    plugin = rt["plugin"] if exact else plugin_id(meta["name"])
    print()
    print(f"  {meta['name']}  (loose pak -> Dresscode"
          + (f", {len(outfits)} outfits" if len(outfits) > 1 else "") + ")")
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
    print(f"      -> {os.path.join(os.path.dirname(source), f'{plugin} (Dresscode)', plugin)}{os.sep}"
          f"   (goes into End\\Mods)")
    for k, o in enumerate(outfits):
        pic = (os.path.basename(o["preview"]) if o["preview"]
               else "no picture (optional)")
        print(f"      {k + 1}. {o['name']}   [{pic}]")
    print()
    if not confirm(assume_yes, len(outfits)):
        print("  Nothing converted.")
        return 0

    # A wrapper folder, so the output can never land on (and overwrite) an
    # existing copy of the mod -- roundtrips make that collision routine. The
    # folder INSIDE keeps the exact plugin name Dresscode requires.
    out_root = os.path.join(os.path.dirname(source), f"{plugin} (Dresscode)")
    if exact:
        root = mkdc.restore(rt, {o["folder"]: o["utoc"] for o in outfits},
                            out_root)
    else:
        root = mkdc.build(meta, outfits, plugin, out_root)
    written = os.path.join(root, "Content", "Paks", "WindowsNoEditor",
                           f"{plugin}End-WindowsNoEditor.utoc")
    problems = rename.verify(written)
    if problems:
        print()
        print("  PROBLEM -- the converted mod is not sound, do not install it:")
        for p in problems[:8]:
            print(f"    {p}")
        return 1
    print(f"    checked  {os.path.basename(written)} is internally consistent")
    print()
    print(f"  Done. Copy the \"{plugin}\" folder from inside "
          f"\"{os.path.basename(out_root)}\" into End\\Mods.")
    return 0


def folder_name(text):
    """`text` reduced to something Windows accepts as a folder name."""
    cleaned = "".join(" " if c in '<>:"/\\|?*' or ord(c) < 32 else c
                      for c in text)
    return " ".join(cleaned.split()).strip(" .")


def prepare_to_loose(toc, uplugin, out_base=None):
    """
    Plan a mod's conversions and print their summaries. Returns a list of
    zero-argument callables, one per variant -- planning is separated from
    writing so a multi-mod drop can show everything before one confirmation.

    A single-outfit mod writes straight into "<Mod> (loose pak)". Variants
    each get a sub-folder inside it, named for the outfit, and every variant
    keeps the same file name -- they all replace the same stock costume, so
    installing one over another in ~mods swaps them cleanly.

    `out_base` overrides where the output folder goes: beside the mod's own
    folder normally, but a mod unpacked from an archive lives in a temp
    folder, so its output belongs beside the archive instead.
    """
    plugin = os.path.splitext(os.path.basename(uplugin))[0]
    plans, ctx = plan_variants(toc, plugin)

    source_root = os.path.abspath(os.path.dirname(uplugin)).rstrip("\\/")
    mod_out = (os.path.join(out_base, os.path.basename(source_root) + " (loose pak)")
               if out_base else source_root + " (loose pak)")
    base = f"{plugin}_P"

    # One scannable block per mod: what it is, where it goes, and the variant
    # list -- per-variant paths and package details would drown a multi-mod
    # drop. Characters are named per variant only when they differ.
    n = len(plans)
    chars = [o["player_type"].split("::")[-1].title() for o, *_ in plans]
    mixed = len(set(chars)) > 1
    head = f"  {plugin}  (Dresscode"
    if n > 1:
        head += f", {n} outfits"
    if not mixed:
        head += f", replaces {chars[0]}'s standard outfit"
    print()
    print(head + ")")
    print(f"      -> {mod_out}{os.sep}")

    runners, used, layout = [], set(), []
    for k, (outfit, target, renames, objects, drop) in enumerate(plans):
        if n == 1:
            out_dir, label = mod_out, os.path.basename(mod_out)
        else:
            # Named for the outfit; authors reuse display names across
            # variants, so a clash falls back to the mesh's own name.
            sub = folder_name(outfit["name"])
            if not sub or sub.lower() in used:
                mesh_leaf = outfit["skeletal_mesh"].split(".")[-1]
                sub = folder_name(f"{outfit['name']} ({mesh_leaf})".strip())
            used.add(sub.lower())
            out_dir, label = os.path.join(mod_out, sub), sub
            print(f"      {k + 1}. {sub}"
                  + (f"   ({chars[k]})" if mixed else ""))
        layout.append(("." if n == 1 else label,
                       (outfit, target, renames, objects, drop)))

        def run(renames=renames, objects=objects, drop=drop,
                out_dir=out_dir, label=label):
            written = rename.rename_container(toc, renames, CONTAINER_MOUNT,
                                              loose_path, out_dir, base,
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

    def record():
        return record_roundtrip(toc, uplugin, plugin, plans, ctx, mod_out,
                                layout)

    runners.append(record)
    return runners


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
        for utoc, uplugin in find_mods(tmp):
            yield utoc, uplugin, os.path.dirname(source)
        return

    for utoc, uplugin in find_mods(source):
        yield utoc, uplugin, None
    for arc in drops.archives_in(source):
        yield from gather(arc, temps)


def main(argv):
    global KEEP_REGISTRATION
    args = [a for a in argv if not a.startswith("-")]
    assume_yes = "-y" in argv or "--yes" in argv
    KEEP_REGISTRATION = "--keep-registration" in argv
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
                    handled = loose_to_dresscode(source, find_mods(source),
                                                 assume_yes)
                except RuntimeError as ex:
                    print(f"  {ex}")
                    code = max(code, 1)
                    continue
                if handled is not None:
                    handled_any = True
                    code = max(code, handled)
                    continue
            found = False
            for utoc, uplugin, out_base in gather(source, temps):
                found = True
                if not uplugin:
                    print()
                    print(f"  {os.path.basename(utoc)}  (loose pak -- drop "
                          "its own folder to convert toward Dresscode)")
                    code = max(code, 1)
                    continue
                try:
                    toc = iostore.Toc(utoc)
                    tocs.append(toc)
                    runners += prepare_to_loose(toc, uplugin, out_base)
                except RuntimeError as ex:
                    print(f"  {os.path.basename(utoc)}: {ex}")
                    code = max(code, 1)
                except Exception as ex:
                    print(f"  Could not read {os.path.basename(utoc)}: {ex}")
                    code = max(code, 1)
            if not found:
                print(f"  No mod container (.utoc) found in {source}")
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
        print("  Done. Copy each mod's three files into End\\Content\\Paks\\~mods.")
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
    drops.pause_before_exit(sys.argv[1:], _INTERACTED)
    sys.exit(_code)
