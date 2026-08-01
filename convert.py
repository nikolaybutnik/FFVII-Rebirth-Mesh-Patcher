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

import cityhash                                                 # noqa: E402
import conheader                                                # noqa: E402
import dirindex                                                 # noqa: E402
import drops                                                    # noqa: E402
import iostore                                                  # noqa: E402
import matpack                                                  # noqa: E402
import mkdc                                                     # noqa: E402
import moddata                                                  # noqa: E402
import pakfile                                                  # noqa: E402
import pngfile                                                  # noqa: E402
import rename                                                   # noqa: E402
import texread                                                  # noqa: E402
import toggles                                                  # noqa: E402
import writer                                                   # noqa: E402
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

    Outfit rows with an ACTOR are Dresscode toggles (hide the jacket, recolor
    the wings): a blueprint applying an EndMaterialPack that swaps mesh
    material slots. Those become OPTIONAL paks in the pre-Dresscode modular
    style -- a tiny pak overriding the swapped material outright, dropped in
    ~mods next to the base at the user's choice. The blueprint and pack
    themselves are meaningless outside Dresscode and travel only in the
    round-trip record.

    Returns (plans, toggles, ctx): plans is [(outfit, target, renames,
    objects, drop)] per variant, toggles one dict per optional pak.
    """
    assets = moddata.find_data_assets(toc)
    if "character" not in assets:
        raise RuntimeError("no Dresscode outfit data in this mod")
    outfits = moddata.read_outfits(toc.read(assets["character"]))
    meshed = [o for o in outfits
              if o["skeletal_mesh"]
              and not o["skeletal_mesh"].startswith("/Game/")]
    # Rows with an actor are Dresscode toggles. Their mesh is usually None
    # ("apply to whatever is worn"); one with a mesh of its own is promoted
    # to a base outfit too, so its look still ships.
    toggle_rows = [o for o in outfits if o.get("actor")]
    base_meshes = {o["skeletal_mesh"].split(".")[0].lower()
                   for o in meshed if not o.get("actor")}
    wearable = []
    for o in meshed:
        mesh_low = o["skeletal_mesh"].split(".")[0].lower()
        if not o.get("actor"):
            wearable.append(o)
        elif mesh_low not in base_meshes:
            base_meshes.add(mesh_low)
            wearable.append(o)
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
        if o in wearable or o in toggle_rows or not o["preview_image"]:
            continue
        p = by_name.get(o["preview_image"].split(".")[0].lower())
        if p is not None and p not in preview_users \
                and not any(p in c for c in closures):
            orphan_previews.add(p)

    # The base outfits a mesh-less toggle may apply to. Slot names repeat
    # across meshes (every outfit has a hair slot), so a toggle sharing its
    # NAME with a base row belongs to that row alone.
    bases = []
    for o in wearable:
        pid = by_name.get(o["skeletal_mesh"].split(".")[0].lower())
        target = moddata.default_costume_package(o["player_type"])
        if pid is not None and target:
            bases.append((pid, target.rsplit("/Model/", 1)[0],
                          (o["name"] or "").strip()))

    base_union = set().union(*closures) if closures else set()
    toggles, blob_only = plan_toggles(
        toc, plugin, packages, by_name, deps, closure, base_union,
        set(preview_users), toggle_rows, registration, bases)

    # Cargo that IMPORTS the Dresscode-only machinery -- or anything only
    # SOME variants carry -- is Dresscode-only machinery itself: parent
    # blueprints, helper assets. It would dangle in every variant missing
    # its target, and it does nothing in a loose pak anyway. Walk until no
    # unclaimed package still references anything unsafe.
    reg_pids = {pid for pid, p in packages.items()
                if p["chunk"] in registration}
    toggle_keep = set().union(*(t["keep"] for t in toggles)) \
        if toggles else set()
    common = set.intersection(*closures) if closures else set()
    variant_specific = (base_union - common) | set(preview_users)
    removed = reg_pids | blob_only | toggle_keep
    changed = True
    while changed:
        changed = False
        doomed = {cityhash.object_id(packages[pid]["name"], path)
                  for pid in removed | variant_specific if pid in packages
                  for path in packages[pid]["exports"]}
        for pid, info in packages.items():
            if pid in removed or pid in base_union:
                continue
            z = zen.ZenPackage(toc.read(info["chunk"]))
            if any(imp in doomed for imp in z.imports):
                blob_only.add(pid)
                removed.add(pid)
                changed = True

    toggle_owned = toggle_keep | blob_only

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
            | orphan_previews | (toggle_owned - closures[k])

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
    return plans, toggles, dict(packages=packages, assets=assets,
                                outfits=outfits, blob_only=blob_only)


def converted_name(name, plugin, costume_root):
    """Where a mod package lands in the loose layout; stock names stay put."""
    if name.lower().startswith(f"/{plugin.lower()}/"):
        return f"{costume_root}/{plugin}{name[len(plugin) + 1:]}"
    return name


def plan_toggles(toc, plugin, packages, by_name, deps, closure, base_union,
                 base_previews, toggle_rows, registration, bases):
    """
    One optional-pak plan per distinct material swap.

    Follows actor -> blueprint -> EndMaterialPack -> {slot: replacement},
    reads a mesh's slot->material table, and turns each swap into a package
    override: the replacement material renamed onto the material package the
    base pak serves for that slot. A toggle row usually names no mesh
    ("apply to whatever is worn"), so its pack is tried against every base
    outfit and lands where its slot names match. A slot whose material
    package is SHARED with slots the pack does not touch cannot be
    overridden without side effects and is skipped with a note.
    """
    reg_pids = {pid for pid, p in packages.items()
                if p["chunk"] in registration}
    toggles, blob_only = [], set()
    resolver = None
    slots_cache = {}
    for row in toggle_rows:
        bp_pkg = row["actor"].split(".")[0]
        bp_pid = by_name.get(bp_pkg.lower())
        if bp_pid is None:
            continue
        blob_only.add(bp_pid)
        pack_pid = next(
            (p for p in deps.get(bp_pid, []) if p in packages
             and matpack.is_material_pack(toc.read(packages[p]["chunk"]))),
            None)
        if pack_pid is None:
            print(f"      note: {row['name'] or bp_pkg} does something no "
                  "loose pak can (not a material swap) -- Dresscode only")
            continue
        blob_only.add(pack_pid)
        if resolver is None:
            resolver = matpack.object_resolver(toc)
        pack = matpack.read_material_pack(toc.read(packages[pack_pid]["chunk"]))

        own_mesh = (row["skeletal_mesh"] or "").split(".")[0]
        own_pid = by_name.get(own_mesh.lower()) if own_mesh else None
        if own_pid is not None:
            candidates = [b for b in bases if b[0] == own_pid]
        else:
            row_name = (row["name"] or "").strip()
            named = [b for b in bases if row_name and b[2] == row_name]
            candidates = named or bases

        matched = False
        for mesh_pid, costume_root, _bname in candidates:
            if mesh_pid not in slots_cache:
                slots_cache[mesh_pid] = matpack.material_slots(
                    toc.read(packages[mesh_pid]["chunk"]))
            slots = slots_cache[mesh_pid]
            slot_to_base = {s: imp for s, imp, _o in slots}

            # swaps: {slot: (base (pkg, obj), repl (pkg, obj), shared)}. A
            # slot whose base material package also serves slots the pack
            # does not touch cannot be swapped by overriding the package --
            # the pack's OTHER slots usually give an anchor: repoint the
            # shared slot at a clean slot's material import (a 4-byte mesh
            # patch to an EXISTING import) and let that slot's package
            # override do the rest. Only a shared slot with no anchor needs
            # the mesh to learn a brand-new import.
            swaps, skipped = {}, []
            for slot, repl_imp in pack.items():
                base_imp = slot_to_base.get(slot)
                base = resolver.get(base_imp) if base_imp else None
                repl = resolver.get(repl_imp)
                if not base or not repl:
                    skipped.append(slot)
                    continue
                if base[0].lower() == repl[0].lower():
                    continue
                shared = any(s != slot and s not in pack and imp == base_imp
                             for s, imp, _o in slots)
                swaps[slot] = (base, repl, shared)
            if not swaps:
                continue
            matched = True

            overrides, objects, repoint, loose = {}, {}, {}, {}
            for slot, (base, repl, shared) in swaps.items():
                if shared:
                    continue
                overrides[base[0]] = repl[0]
                if base[1] != repl[1]:      # must answer as the base object
                    objects[repl[0].lower()] = {repl[1]: base[1]}
            for slot, (base, repl, shared) in swaps.items():
                if not shared:
                    continue
                anchor = next(
                    (b2 for _s2, (b2, r2, sh2) in swaps.items()
                     if not sh2 and r2[0].lower() == repl[0].lower()), None)
                if anchor is not None:
                    repoint[slot] = anchor      # base (pkg, obj) to reuse
                else:
                    # Would need the mesh to learn a brand-new import --
                    # probed in game and the loader never resolves it, so
                    # be honest instead of shipping a grey part.
                    loose[slot] = repl
            if loose:
                print(f"      note: {row['name'] or bp_pkg}: "
                      f"{', '.join(sorted(loose))} cannot swap in a loose "
                      "pak -- those parts keep their normal look")
            if not overrides and not repoint:
                continue
            kind = "mesh" if repoint else "swap"

            keep = set()
            repl_pids = {by_name[r[0].lower()]
                         for _b, r, _sh in swaps.values()
                         if r[0].lower() in by_name}
            for pid in repl_pids:
                keep |= closure(pid)
            if row["preview_image"]:
                p = by_name.get(row["preview_image"].split(".")[0].lower())
                if p is not None:
                    keep |= closure(p)
            # Shared things stay with the base pak (an optional always
            # rides beside it) -- except the replacements themselves, which
            # the optional must carry regardless, and the mesh when the
            # swap is baked into it.
            keep -= base_union | base_previews | reg_pids | blob_only
            keep |= repl_pids
            if kind == "mesh":
                keep.add(mesh_pid)

            dup = next((t for t in toggles
                        if t["swaps"] == swaps and t["kind"] == kind
                        and (kind == "swap"
                             or t["mesh_pid"] == mesh_pid)), None)
            if dup is not None:
                dup["keep"] |= keep
                dup["bases"].add(mesh_pid)
                continue
            # `bases` is every outfit this one swap works on -- one means it
            # belongs inside that outfit's folder, several mean it is general.
            toggles.append(dict(
                row=row, bp=bp_pkg, kind=kind, swaps=swaps,
                overrides=overrides, objects=objects, repoint=repoint,
                loose=loose, skipped=skipped, keep=keep, mesh_pid=mesh_pid,
                bases={mesh_pid}, costume_root=costume_root))
        if not matched:
            print(f"      note: {row['name'] or bp_pkg}: its material swap "
                  "has no effect a loose pak can carry -- Dresscode only")
    return toggles, blob_only


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


def carries_outfit(utoc):
    """Whether a pak replaces a character's standard costume -- an outfit.
    Anything else is an extra: the modular standard's optional paks override
    a material or the mask a material samples, and never the mesh."""
    try:
        toc = iostore.Toc(utoc)
    except Exception:
        return False
    try:
        return bool(mkdc.find_stock_mesh(rename.read_packages(toc))[0])
    except Exception:
        return False
    finally:
        toc.close()


def loose_layout(source, mods):
    """
    (mod_name, [(relative folder, utoc)], [extra utoc], [companion utoc])
    when `source` is the root of a loose pak mod in a shape the template
    supports -- else None.

    Outfits are the paks carrying a costume. Everything else under an
    Optional folder is an extra (a variant, opt-in); everything else
    ELSEWHERE is a companion -- a REQUIRED pak the outfit cannot render
    without (authors routinely ship the mesh in one pak and its materials
    and textures in another). Companions merge into every outfit.
    """
    if not mods or any(uplugin for _utoc, uplugin in mods):
        return None
    root = os.path.normcase(os.path.abspath(source))
    outfits, extras, companions = [], [], []
    for utoc, _ in mods:
        folders = os.path.relpath(utoc, source).lower().split(os.sep)[:-1]
        # An Optional folder wins over content: a generated Optional tree's
        # paks DO carry their outfit's mesh, and stay extras.
        if "optional" in folders:
            extras.append(utoc)
        elif carries_outfit(utoc):
            outfits.append(utoc)
        else:
            companions.append(utoc)
    if not outfits:
        return None

    parts, by_folder = [], {}
    for utoc in outfits:
        d = os.path.dirname(os.path.abspath(utoc))
        if os.path.normcase(d) != root \
                and os.path.normcase(os.path.dirname(d)) != root:
            return None                 # deeper nesting; not a shape we know
        by_folder.setdefault(os.path.normcase(d), []).append(utoc)
    for utoc in outfits:
        d = os.path.dirname(os.path.abspath(utoc))
        at_root = os.path.normcase(d) == root
        if at_root and len(outfits) > 1:
            return None                 # several outfits loose in one folder
        # Several outfits sharing a folder are named for their own paks.
        if len(by_folder[os.path.normcase(d)]) > 1:
            parts.append((os.path.splitext(os.path.basename(utoc))[0], utoc))
        else:
            parts.append(("." if at_root else os.path.basename(d), utoc))
    if len({p == "." for p, _ in parts}) > 1:
        return None                     # a pak both at the root and in subs
    name = os.path.basename(os.path.abspath(source).rstrip("\\/"))
    if name.lower().endswith(" (loose pak)"):
        name = name[:-len(" (loose pak)")]
    return name, sorted(parts), sorted(extras), sorted(companions)


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


def write_template(source, mod_name, parts, prefill=None, restore=None,
                   extras=()):
    """Prefill dresscode.json with the little a build needs. Pictures are on
    purpose NOT in here -- they are picked up by where they sit.

    `prefill` carries real values when the loose mod was itself converted
    from a Dresscode mod; `restore` is that conversion's opaque record of the
    original, which makes converting back exact. `extras` is [(label, pak
    stem)] for the mod's optional paks: each starts as one variant, and the
    person can compose their own from several."""
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
            "Several outfits build several Dresscode mods, one per outfit",
            "(named \"mod - outfit\"), each with its own toggles.",
            "",
            "\"stackable\": true builds the COMBINABLE form instead. The",
            "extras do not become menu toggles; the mod's shared masks stay",
            "at their original paths, served from ~mods -- so the original",
            "Optional paks keep working and can be combined freely, while",
            "the outfit itself is picked in Dresscode. The build writes a",
            "\"Put in ~mods\" folder with everything that goes there.",
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
        "stackable": False,
        "outfits": outfits,
    }
    if extras:
        template["_how_this_works"] += [
            "",
            "\"variants\" is the menu below each outfit. One entry = one",
            "menu item; its \"parts\" list the extra paks it applies, BY",
            "FILE NAME. Compose your own by listing several parts in one",
            "entry -- when two parts change the same thing, the later one",
            "wins. Rename, reorder, or delete entries freely.",
        ]
        template["variants"] = [{"name": label, "parts": [stem]}
                                for label, stem in extras]
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
        stackable=bool(data.get("stackable")),
        variants=data.get("variants"),
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


def safe_plugin_id(name, used):
    """plugin_id, kept unique across one drop -- two outfits whose names
    differ only in punctuation would otherwise collide."""
    base = plugin_id(name)
    out, n = base, 1
    while out.lower() in used:
        n += 1
        out = f"{base}{n}"
    used.add(out.lower())
    return out


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
        got = md5_of(o.get("preview"))
        want = by_folder.get(o["folder"])
        # A single-outfit mod lives at the root, beside the mod's icon --
        # with no preview of its own, the outfit picks the icon up as its
        # folder's image. Seeing the icon twice is not an edit.
        if got != want and not (want is None and got == rt.get("icon_md5")):
            return False
    return md5_of(meta.get("icon")) == rt.get("icon_md5")


def record_roundtrip(toc, uplugin, plugin, plans, ctx, mod_out, layout,
                     opt_layout=()):
    """
    Leave everything beside the written loose paks that converting BACK will
    need: a prefilled dresscode.json, each outfit's preview as a PNG a person
    can see and swap, the plugin icon -- and the opaque restore record.

    `layout` is [(rel_folder, plan)], "." meaning the mod's root folder;
    `opt_layout` is [(label, toggle)] for the generated Optional paks.
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
    for rel, _stem, t in opt_layout:
        ref = t["row"].get("preview_image")
        pid = by_name.get(ref.split(".")[0].lower()) if ref else None
        if pid in packages:
            put_png(os.path.join(mod_out, *rel.split("/")), "preview",
                    packages[pid]["chunk"])

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
        entry_of[pid] = dict(
            exp=exp, bun=bun, lo=lo, pad=pad, order=j,
            deps=[str(d) for d in
                  conheader.imported_packages(hdr, info, j)])

    name_of = {pid: p["name"] for pid, p in packages.items()}
    chunk_order = []
    for i in range(toc.n):
        t = toc.chunk_ids[i][11]
        pid = int.from_bytes(toc.chunk_ids[i][:8], "little")
        chunk_order.append(["" if t == 10 else name_of.get(pid, ""), t])
    # The directory index VERBATIM, aligned with chunk_order. Deriving
    # paths from package names loses the cooker's file-name casing (a
    # package whose name table says "pink" can sit in the index as
    # "Pink.uasset"), and a restore must not.
    dir_paths = [toc.paths.get(i, "") for i in range(toc.n)]

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

    # The registration assets plus the toggle blueprints and material packs
    # travel as verbatim bytes -- they are Dresscode-only machinery no loose
    # pak may carry. So does any Optional-pak package whose name table
    # mentions an overridden base package: the override collapses two names
    # into one, and no inverse map can pull them apart again.
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
        name_of[pid]: base64.b64encode(
            zlib.compress(toc.read(packages[pid]["chunk"]), 9)).decode("ascii")
        for pid in stored_pids if pid in packages}

    # The completeness net: anything neither carried by a loose pak nor
    # stored above would be unrecoverable. Seen in the wild: alternative
    # hair meshes only a toggle actor references, spare icon textures.
    # Their bulk data rides too -- a mesh has .ubulk.
    carried = set()
    for _rel, (_o, _t, _ren, _obj, drop) in layout:
        dropped = {d.lower() for d in (drop or ())}
        carried |= {p["name"].lower() for p in packages.values()} - dropped
    stored_bulks = {}
    for pid, p in packages.items():
        if p["name"].lower() in carried or name_of[pid] in stored_chunks:
            continue
        stored_chunks[name_of[pid]] = base64.b64encode(
            zlib.compress(toc.read(p["chunk"]), 9)).decode("ascii")
        blobs = []
        for i in range(toc.n):
            cid = toc.chunk_ids[i]
            if cid[11] in (3, 4) and \
                    int.from_bytes(cid[:8], "little") == pid:
                blobs.append([bytes(cid).hex(), base64.b64encode(
                    zlib.compress(toc.read(i), 9)).decode("ascii")])
        if blobs:
            stored_bulks[name_of[pid]] = blobs

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
                    converted_name(base_pkg, plugin, t["costume_root"]))
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
        back = {converted_name(p["name"], plugin,
                               t["costume_root"]).lower(): p["name"]
                for p in packages.values()}
        for base_pkg, repl_pkg in t["overrides"].items():
            back[converted_name(base_pkg, plugin,
                                t["costume_root"]).lower()] = repl_pkg
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

    record = dict(
        plugin=plugin,
        mount=toc.mount,
        cid=str(toc.container_id),
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
                   restore=pack_restore(record),
                   extras=[(rel.split("/")[-1], str(v.get("pak", "")))
                           for rel, v in optionals.items()])
    print("    recorded  dresscode.json + pictures -- converting the folder "
          "back restores this mod exactly")
    return 0


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
                f"cannot place {package_name} in a loose pak")
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


def merge_loose(utocs, out_dir, base):
    """
    One loose container carrying every package of `utocs` -- an outfit that
    ships as several REQUIRED paks (the mesh in one, its materials and
    textures in another) becomes a single container the conversion treats
    as THE outfit. Package bytes, names, arcs and bulk data are carried
    unchanged; only the container header and directory are new. The first
    pak wins a package two of them carry. Returns the merged .utoc path.
    """
    tocs = [iostore.Toc(u) for u in utocs]
    template = tocs[0]
    merged, order = {}, []
    for toc in tocs:
        packages = rename.read_packages(toc)
        hdr = next(toc.read(i) for i in range(toc.n)
                   if toc.chunk_ids[i][11] == 10)
        info = conheader.parse(hdr)
        entry_meta = {}
        for j, pid in enumerate(conheader.package_ids(hdr, info)):
            _sz, exp, bun = struct.unpack_from(
                "<Qii", hdr, info["store_off"] + j * 32)[:3]
            entry_meta[pid] = (exp, bun,
                               conheader.imported_packages(hdr, info, j))
        bulks = {}
        for i in range(toc.n):
            if toc.chunk_ids[i][11] in (3, 4):
                pid = int.from_bytes(toc.chunk_ids[i][:8], "little")
                bulks.setdefault(pid, []).append(i)
        for pid, pkg in packages.items():
            if pid in merged:
                continue
            exp, bun, deps = entry_meta.get(pid, (1, 1, []))
            merged[pid] = dict(name=pkg["name"], toc=toc, data=toc.read(
                pkg["chunk"]), exp=exp, bun=bun, deps=list(deps),
                bulks=bulks.get(pid, []))
            order.append(pid)

    cid = cityhash.package_id(base)
    hdr_out = struct.pack("<QIIIIQ", cid, len(order), 0, 0, 8, 0xC1640000)
    hdr_out += struct.pack("<I", len(order))
    hdr_out += b"".join(p.to_bytes(8, "little") for p in order)
    store = bytearray()
    for j, pid in enumerate(order):
        rec = merged[pid]
        store += struct.pack("<QiiII", len(rec["data"]), rec["exp"],
                             rec["bun"], j, 0xFFFFFFFF)
        store += struct.pack("<II", 0, 0)
    for j, pid in enumerate(order):
        rec = merged[pid]
        view = j * 32 + 24
        if rec["deps"]:
            struct.pack_into("<II", store, view, len(rec["deps"]),
                             len(store) - view)
            store += struct.pack(f"<{len(rec['deps'])}Q", *rec["deps"])
    hdr_out += struct.pack("<I", len(store)) + store
    if len(hdr_out) % 65536:
        hdr_out += b"\0" * (65536 - len(hdr_out) % 65536)

    comp = next((m for m, n in enumerate(template.methods)
                 if n.lower() == "oodle"), None)

    def blocks_of(payload):
        return rename.pack_blocks(payload, template.block_size, comp)

    chunks = [dict(id=cid.to_bytes(8, "little") + b"\0\0\0\x0a",
                   blocks=blocks_of(hdr_out), size=len(hdr_out))]
    payloads = [hdr_out]
    paths = []
    for pid in order:
        rec = merged[pid]
        if not rec["name"].lower().startswith("/game/"):
            raise RuntimeError(
                f"cannot merge {rec['name']} -- not under /Game/")
        rel = rec["name"][len("/Game/"):]
        chunks.append(dict(id=pid.to_bytes(8, "little") + b"\0\0\0\x02",
                           blocks=blocks_of(rec["data"]),
                           size=len(rec["data"])))
        payloads.append(rec["data"])
        paths.append((rel + ".uasset", len(chunks) - 1))
        for i in rec["bulks"]:
            data = rec["toc"].read(i)
            chunks.append(dict(id=bytes(rec["toc"].chunk_ids[i]),
                               blocks=blocks_of(data), size=len(data)))
            payloads.append(data)
            ext = ".uptnl" if rec["toc"].chunk_ids[i][11] == 4 else ".ubulk"
            paths.append((rel + ext, len(chunks) - 1))

    directory = dirindex.build_dir_index("../../../End/Content/", paths)
    body, ucas, _offlen, block_table = writer.build_container(
        template, chunks, template.block_size)
    head = bytearray(writer.build_toc_header(
        template, len(chunks), len(block_table), len(directory),
        template.block_size))
    struct.pack_into("<Q", head, 0x38, cid)
    metas = b"".join(hashlib.sha1(p).digest() + b"\0" * 12 + b"\x01"
                     for p in payloads)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, base + ".utoc"), "wb") as f:
        f.write(bytes(head) + bytes(body) + directory + metas)
    with open(os.path.join(out_dir, base + ".ucas"), "wb") as f:
        f.write(ucas)
    with open(os.path.join(out_dir, base + ".pak"), "wb") as f:
        f.write(pakfile.build(pakfile.LOOSE_MOUNT))
    for toc in tocs:
        toc.close()
    return os.path.join(out_dir, base + ".utoc")


def resolve_variants(meta, extras):
    """
    [(row name, [utoc, ...])] from the template's "variants" section --
    or one row per extra when the section is absent. Also says whether the
    person edited the section away from the generated default, which must
    override an exact-restore record: an edit means they WANT the change.
    """
    def stem(u):
        return os.path.splitext(os.path.basename(u))[0]

    by_stem = {stem(u).lower(): u for u in extras}
    cfg = meta.get("variants")
    default = [(toggles.label_of(u), [u]) for u in extras]
    if cfg is None:
        return default, False
    if not isinstance(cfg, list):
        raise RuntimeError(f'{TEMPLATE}: "variants" must be a list')
    out = []
    for k, entry in enumerate(cfg):
        if not isinstance(entry, dict):
            raise RuntimeError(f'{TEMPLATE}: variant {k + 1} must be an '
                               'object with "name" and "parts"')
        utocs = []
        for s in entry.get("parts") or []:
            key = os.path.splitext(str(s))[0].lower()
            u = by_stem.get(key)
            if not u:
                raise RuntimeError(
                    f'{TEMPLATE}: variant {k + 1} names a pak that is not '
                    f'there: "{s}". The paks are: '
                    + ", ".join(sorted(by_stem)) + ".")
            utocs.append(u)
        if not utocs:
            continue                    # an empty entry is just skipped
        name = str(entry.get("name") or "").strip() \
            or " + ".join(toggles.label_of(u) for u in utocs)
        out.append((name, utocs))
    # Structural comparison only, as SETS -- display names differ by which
    # conversion wrote the template, and the two writers list the same
    # entries in different orders; a restore reproduces the original's
    # names and order regardless. Only recomposition means they want a
    # different mod: entries added, removed, or made of different parts.
    edited = {tuple(sorted(u.lower() for u in us)) for _n, us in out} != \
             {tuple(sorted(u.lower() for u in us)) for _n, us in default}
    return out, edited


def loose_to_dresscode(source, mods, assume_yes=False):
    """
    The template flow for a dropped loose pak mod. Returns an exit code, or
    None when `source` is not a loose mod root this direction understands.
    """
    layout = loose_layout(source, mods)
    if layout is None:
        return None
    mod_name, parts, extras, companions = layout

    if not os.path.exists(os.path.join(source, TEMPLATE)):
        path = write_template(
            source, mod_name, parts,
            extras=[(toggles.label_of(u),
                     os.path.splitext(os.path.basename(u))[0])
                    for u in extras])
        print()
        print(f"  {mod_name}  (loose pak -> Dresscode)")
        print(f"      created  {os.path.basename(path)}"
              f"  ({len(parts)} outfit{'s' if len(parts) > 1 else ''})")
        print("      Drop the folder again to build. Before that, if you")
        print(f"      want, open {TEMPLATE} to set names and author, and put")
        print("      a picture in an outfit's folder to give it a preview.")
        return 0

    meta, outfits = read_template(source, parts)
    variants, variants_edited = resolve_variants(meta, extras)
    rt = unpack_restore(meta.get("restore"))
    exact = rt is not None and restore_matches(rt, meta, outfits) \
        and not variants_edited
    plugin = rt["plugin"] if exact else plugin_id(meta["name"])
    # Several outfits, each with its own toggles, in one mod make a menu
    # where nothing says which toggle belongs to which outfit -- so each
    # outfit becomes a Dresscode mod of its own. A restore is exempt: it
    # reproduces the original mod, whatever shape that was.
    split = not exact and len(outfits) > 1
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
    stackable = meta["stackable"] and bool(extras) and not exact
    if companions and not exact:
        print(f"      + {len(companions)} required companion pak"
              f"{'s' if len(companions) != 1 else ''} merged into every "
              "outfit")
    if extras and not exact:
        if stackable:
            print(f"      + {len(extras)} extra paks stay COMBINABLE: the "
                  "shared masks ride in ~mods")
            print("        and the original Optional paks keep working on "
                  "top of the Dresscode outfit")
        else:
            combos = sum(1 for _n, us in variants if len(us) > 1)
            print(f"      + {len(variants)} variant"
                  f"{'s' if len(variants) != 1 else ''} from "
                  f"{len(extras)} extra pak{'s' if len(extras) != 1 else ''}"
                  + (f" ({combos} composed)" if combos else ""))
    print()
    if not confirm(assume_yes, len(outfits)):
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
        roots = [mkdc.restore(rt, {o["folder"]: o["utoc"] for o in outfits},
                              out_root, optionals=opt)]
    else:
        if companions:
            # The outfit cannot render without them, so from here on the
            # merged container IS the outfit.
            merge_tmp = os.path.join(out_root, "_merge_tmp")
            for k, o in enumerate(outfits):
                o["utoc"] = merge_loose([o["utoc"]] + list(companions),
                                        merge_tmp, f"Merged{k + 1}_P")
        ex = [] if stackable else variants
        ext = stack_tree(outfits, extras) if stackable else ()
        used, roots = set(), []
        for o in (outfits if split else [None]):
            if split:
                sub_name = f"{meta['name']} - {o['name']}"
                sub = safe_plugin_id(sub_name, used)
                roots.append(mkdc.build(dict(meta, name=sub_name), [o],
                                        sub, out_root, extras=ex,
                                        external=ext))
            else:
                roots.append(mkdc.build(meta, outfits, plugin, out_root,
                                        extras=ex, external=ext))
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
              f"({len(ext)} shared packages, verified)")
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
        problems = rename.verify(written)
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
              f"\"{os.path.basename(out_root)}\" into End\\Mods:")
        for root in roots:
            print(f"    {os.path.basename(root)}")
    else:
        print(f"  Done. Copy the \"{os.path.basename(roots[0])}\" folder "
              f"from inside \"{os.path.basename(out_root)}\" into End\\Mods.")
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
    plans, toggles, ctx = plan_variants(toc, plugin)

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
        opt_base = f"0{chr(65 + (k % 26))}_{plugin_id(label)}_P"
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
                    converted_name(base_pkg, plugin, t["costume_root"]))
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
                       converted_name(p["name"], plugin, t["costume_root"])
                       for p in packages.values()}
            for base_pkg, repl_pkg in t["overrides"].items():
                renames[repl_pkg.lower()] = converted_name(
                    base_pkg, plugin, t["costume_root"])
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
            print(f"          skipped: {label} touches paths outside /Game/")
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

    if len(plans) > 1 or opt_layout:
        print("      install: one outfit folder's three files go in ~mods; "
              "extras from its Optional folder go in alongside")

    def record():
        return record_roundtrip(toc, uplugin, plugin, plans, ctx, mod_out,
                                layout, opt_layout)

    runners.append(record)
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
        print("  Done. Copy the three files from the folder you want into "
              "End\\Content\\Paks\\~mods.")
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
