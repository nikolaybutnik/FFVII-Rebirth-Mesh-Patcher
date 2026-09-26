"""
analyse.py -- reading a Dresscode mod and planning what comes out of it.

Finding a mod's files, the other plugins it leans on, and the two plans a
conversion to paks is built from: one per costume variant, one per
menu toggle. Nothing here writes anything.
"""
import glob
import os

import cityhash
import conheader
import matpack
import mkdc
import moddata
import rename
import weapons
import zen

# The CONTAINER mounts at the player-character folder, exactly like every
# pak mod confirmed working in game -- root mounts exist in the wild but
# none has been verified, so we do not pioneer one. Every converted package
# lands under this folder. The .pak is mounted separately and shallowly; see
# pakfile.LOOSE_MOUNT, which is not this.
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
    reads and what a pak has no equivalent of. A dropped folder can hold
    several mods; each .uplugin is one, and a .utoc with no .uplugin above it
    is a pak.
    """
    if os.path.isfile(path):
        utoc = find_container(path)
        return [(utoc, find_uplugin(utoc))] if utoc else []
    found, seen = [], set()
    for root, dirs, files in os.walk(path):
        # The patcher's undo copies are not part of the mod -- scanned in,
        # they double every outfit with its pre-patch self.
        dirs[:] = [d for d in dirs
                   if d.lower() not in ("_patch_backups", "backups")]
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


def actor_mesh(toc, packages, by_name, resolve, probed, actor):
    """
    The SkeletalMesh an actor blueprint equips, as "package.object" -- the
    mesh of a row that carries no mesh path of its own. `probed` caches
    which packages hold a mesh export; a 100 MB mesh decompresses once.
    """
    pid = by_name.get(str(actor).split(".")[0].lower())
    if pid is None:
        return None
    z = zen.ZenPackage(toc.read(packages[pid]["chunk"]))
    for imp in z.imports:
        tgt = resolve.get(imp)
        if not tgt:
            continue
        tp = by_name.get(tgt[0].lower())
        if tp is None:
            continue
        if tp not in probed:
            zz = zen.ZenPackage(toc.read(packages[tp]["chunk"]))
            probed[tp] = {e["name"] for e in zz.exports
                          if e["cls"] == mkdc.SKELETAL_MESH}
        if tgt[1] in probed[tp]:
            return f"{tgt[0]}.{tgt[1]}"
    return None


def foreign_roots(toc, plugin):
    """
    Other PLUGINS this container HARD-IMPORTS from -- dependencies no
    manifest declares (a shared skin library). Only packages named in the
    container header's dependency lists count: name tables are full of
    benign leftovers (soft paths into the author's other mods, uncooked
    project folders) that the mod demonstrably lives without.
    """
    skip = {plugin.lower(), "game", "script", "engine", "ff7rml", "dresscode"}
    hard = set()
    for pids in header_deps(toc).values():
        hard.update(pids)
    roots = {}
    for p in rename.read_packages(toc).values():
        for n in zen.ZenPackage(toc.read(p["chunk"])).names:
            if not n.startswith("/") or "/" not in n[1:]:
                continue
            root = n[1:].split("/", 1)[0]
            if root.lower() in skip or root.lower() in roots:
                continue
            if cityhash.package_id(n) in hard:
                roots[root.lower()] = root
    return sorted(roots.values())


def locate_libraries(roots, uplugin):
    """
    ({root: (utoc, uplugin, where)}, [missing]) -- each library found
    beside the dependent mod (anywhere under the folder that holds it) or
    installed in End\\Mods; `where` says which, for the person watching.
    Dresscode's own rule makes the .uplugin name the identity.
    """
    import config
    found, missing = {}, []
    near = os.path.dirname(os.path.dirname(os.path.abspath(uplugin)))
    for root in roots:
        cands = glob.glob(os.path.join(near, "**", f"{root}.uplugin"),
                          recursive=True)
        installed = os.path.join(getattr(config, "MODS_DIR", ""), root,
                                 f"{root}.uplugin")
        if os.path.exists(installed):
            cands.append(installed)
        hit = None
        for c in cands:
            utoc = find_container(os.path.dirname(c))
            if utoc:
                if os.path.normcase(c) == os.path.normcase(installed):
                    where = "installed in End\\Mods"
                else:
                    rel = os.path.relpath(os.path.dirname(c), near)
                    where = f"found beside it: {rel}{os.sep}"
                hit = (utoc, c, where)
                break
        if hit:
            found[root] = hit
        else:
            missing.append(root)
    return found, missing


def plan_variants(toc, plugin, extra_roots=(), keep_registration=False):
    """
    A conversion plan per wearable outfit -- a Dresscode mod can register
    several variants, and each becomes its own pak.

    `extra_roots` names library plugins whose packages ride inside `toc`
    (a merged container): they relocate into the loose layout beside the
    mod's own, each under its own subfolder.

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
    assets = moddata.find_data_assets(toc,
                                      prefer=f"/{plugin.lower()}/")
    outfits = moddata.read_outfits(toc.read(assets["character"])) \
        if "character" in assets else []

    packages = rename.read_packages(toc)
    own = {p["name"].lower() for p in packages.values()}

    # A weapons-menu row converts like a costume one: its mesh takes over a
    # stock package and the rest moves in beside it -- only WHICH stock
    # package differs. Tiles this tool built are the exception: they kept
    # the stock path as their tail, so weapon_tiles_back can put every file
    # back exactly where it came from instead of relocating them.
    own_tiles = 0
    for i in moddata.weapon_lists(toc, weapons.MOD_TYPE_WEAPON):
        for row in moddata.read_outfits(toc.read(i)):
            root = str(row.get("skeletal_mesh") or "").split(".")[0]
            # By its layout, not by what it carries: a tile that replaced
            # the weapon model outright has nothing beneath it.
            if weapons.is_tile_path(root) \
                    or any(n.startswith(root.lower() + "/") for n in own):
                own_tiles += 1
                continue
            outfits.append(dict(row, weapon=True))
    # A mod whose every row is one of those tiles has nothing to plan here
    # and is not empty: weapon_tiles_back hands each one back on its own.
    if not outfits and not own_tiles:
        raise RuntimeError("no Dresscode outfit data in this mod")
    by_name = {p["name"].lower(): pid for pid, p in packages.items()}
    deps = header_deps(toc)

    # Whole mods are authored with every mesh riding INSIDE its row's actor
    # blueprint, the row's own mesh path left empty. Pull the mesh out of
    # the blueprint and the row converts like any other outfit.
    resolve = probed = None
    for o in outfits:
        if o["skeletal_mesh"] or not o.get("actor"):
            continue
        if resolve is None:
            resolve = matpack.object_resolver(toc)
            probed = {}
        m = actor_mesh(toc, packages, by_name, resolve, probed, o["actor"])
        if m:
            o["skeletal_mesh"] = m

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
    seen_weapon_meshes = set()
    for o in meshed:
        mesh_low = o["skeletal_mesh"].split(".")[0].lower()
        # Dresscode lists the same sword once per character so it appears
        # in each weapons menu. A pak only needs one override of that mesh.
        if o.get("weapon"):
            if mesh_low in seen_weapon_meshes:
                continue
            seen_weapon_meshes.add(mesh_low)
        if not o.get("actor"):
            wearable.append(o)
        elif mesh_low not in base_meshes:
            base_meshes.add(mesh_low)
            wearable.append(o)
    if not wearable and not own_tiles:
        raise RuntimeError("this mod registers no mesh of its own to convert")

    # The registration assets are Dresscode's, and only Dresscode's. Carried
    # into a pak they become data assets whose CLASS lives in a plugin
    # that is not mounted when ~mods is -- and the loader still enumerates them
    # at startup. They describe a mod that, in this format, does not exist.
    # ALL of them go -- a weapon-tile list is one more instance of the same
    # class, and one surviving is just as fatal as the costume data.
    registration = set() if keep_registration \
        else moddata.registration_chunks(toc)

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
    # them from a pak. Each rides along in ITS outfit's variant anyway,
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
        target = stock_target(o, own)
        if pid is not None and target:
            bases.append((pid, target.rsplit("/Model/", 1)[0],
                          (o["name"] or "").strip()))

    roots = (plugin, *extra_roots)

    # Cooked with the loader's plugin content inlined, a mod carries copies
    # of FF7RML's own structs and enums. They sit under no root the loose
    # layout can mount and nothing but the registration assets reads them,
    # so they travel in the round-trip record and nowhere else. Without
    # this the conversion stopped dead on the first one.
    foreign = {pid for pid, p in packages.items()
               if not p["name"].lower().startswith("/game/")
               and not any(p["name"].lower().startswith(f"/{r.lower()}/")
                           for r in roots)}

    base_union = set().union(*closures) if closures else set()
    toggles, blob_only, dead_repl = plan_toggles(
        toc, roots, packages, by_name, deps, closure, base_union,
        set(preview_users), toggle_rows, registration, bases)

    # Cargo that IMPORTS the Dresscode-only machinery -- or anything only
    # SOME variants carry -- is Dresscode-only machinery itself: parent
    # blueprints, helper assets. It would dangle in every variant missing
    # its target, and it does nothing in a pak anyway. Walk until no
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

    # dead_repl: variant materials no pak can apply. Owned by nobody, so
    # without naming them here they would settle into the base pak.
    toggle_owned = toggle_keep | blob_only | dead_repl

    plans = []
    for k, outfit in enumerate(wearable):
        mesh = outfit["skeletal_mesh"].split(".")[0]
        if by_name.get(mesh.lower()) is None:
            print(f"  skipping {outfit['name'] or mesh!r}: its model file "
                  "is missing from this mod")
            continue
        target = stock_target(outfit, own)
        if not target:
            if outfit.get("weapon"):
                print(f"  skipping {outfit['name'] or mesh!r}: cannot tell "
                      "which weapon it replaces (unknown character "
                      f"{outfit['player_type']!r})")
            else:
                print(f"  skipping {outfit['name'] or mesh!r}: unknown "
                      f"character {outfit['player_type']!r}")
            continue
        if outfit.get("weapon") and not weapons.stock_for(mesh, own):
            who = outfit["player_type"].split("::")[-1].title()
            print(f"  note: {outfit['name'] or mesh!r} names no stock "
                  f"weapon -- replacing {who}'s default "
                  f"({target.rsplit('/', 1)[-1]})")

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
            if (info["chunk"] in registration or pid in foreign
                    or pid in exclusive_elsewhere):
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
            else:
                renames[name.lower()] = converted_name(name, roots,
                                                       costume_root)
        plans.append((outfit, target, renames, objects, drop))
    if not plans and not own_tiles:
        raise RuntimeError("none of this mod's outfits can be converted")
    # The survey rides along so the round-trip recorder does not pay for a
    # second full read of every package.
    return plans, toggles, dict(packages=packages, assets=assets,
                                outfits=outfits, blob_only=blob_only,
                                roots=roots)


def stock_target(row, own):
    """
    The stock package this row's mesh takes over: a character's default
    costume, or -- for a weapons-menu row -- the weapon it stands in for.
    None when it cannot be told, which the caller reports.
    """
    if not row.get("weapon"):
        return moddata.default_costume_package(row["player_type"])
    mesh = row["skeletal_mesh"].split(".")[0]
    return (weapons.stock_for(mesh, own)
            or weapons.default_weapon_package(row["player_type"]))


def converted_name(name, plugin, costume_root):
    """Where a mod package lands in the loose layout; stock names stay put.
    `plugin` may be one root or several -- a mod's undeclared library
    dependencies relocate right beside it, each under its own subfolder."""
    roots = (plugin,) if isinstance(plugin, str) else plugin
    for r in roots:
        if name.lower().startswith(f"/{r.lower()}/"):
            return f"{costume_root}/{r}{name[len(r) + 1:]}"
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
    toggles, blob_only, dead_repl = [], set(), set()
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
            own_mesh = (row["skeletal_mesh"] or "").split(".")[0]
            own_pid = by_name.get(own_mesh.lower()) if own_mesh else None
            if own_pid is not None and any(b[0] == own_pid for b in bases):
                # The actor only equips a mesh that already converts as a
                # base outfit -- nothing here needs an Optional pak.
                continue
            print(f"      note: the \"{row['name'] or bp_pkg}\" menu item "
                  "is Dresscode-only logic with no pak form -- it is "
                  "kept safe and returns when converted back.")
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

            # TWO SLOTS, ONE PACKAGE, TWO ANSWERS. A mesh may point two slots
            # at the SAME material package, and a Dresscode row may still ask
            # for a different material on each -- it applies materials per
            # slot, so it can. A pak cannot: an override replaces the package
            # both slots read, so both slots change together, and only one
            # replacement can occupy that name.
            #
            # Left alone, `overrides` (keyed by the base package) just kept
            # whichever slot came last, and the other's material vanished --
            # which shipped one slot's material wearing the other's name
            # and rendered the whole costume as grey checkers, in game,
            # with nothing in the output saying so.
            collided = set()
            by_base = {}
            for slot, (base, repl, _sh) in swaps.items():
                by_base.setdefault(base[0].lower(), []).append(
                    (slot, repl[0].lower()))
            for _basepkg, entries in by_base.items():
                if len({r for _s, r in entries}) < 2:
                    continue                # all agree: one override serves
                keeper = sorted(s for s, _r in entries)[0]
                collided.update(s for s, _r in entries if s != keeper)

            overrides, objects, repoint, loose = {}, {}, {}, {}
            for slot, (base, repl, shared) in swaps.items():
                if slot in collided:
                    loose[slot] = repl
                    continue
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
            kind = "mesh" if repoint else "swap"

            # A replacement that IMPORTS the package it replaces (a material
            # instance overriding its own parent) would, renamed, import
            # ITSELF -- and one self-import in a container header stops the
            # game launching at all. Carry the base package in the pak under
            # a side name so the parent chain stays real. When the parent is
            # the game's own package there is no copy to carry, and a
            # mesh-carrying pak needs the base name for the mesh -- both
            # demote to "keeps its normal look" instead.
            carry = set()
            for slot, (base, repl, shared) in swaps.items():
                if shared or slot in collided or slot in loose:
                    continue
                r_pid = by_name.get(repl[0].lower())
                if r_pid is None or cityhash.package_id(base[0]) \
                        not in deps.get(r_pid, ()):
                    continue
                if kind == "swap" and base[0].lower() in by_name:
                    carry.add(base[0])
                else:
                    overrides.pop(base[0], None)
                    objects.pop(repl[0].lower(), None)
                    loose[slot] = repl
            if collided:
                # Said plainly, because the result LOOKS deliberate in game:
                # the part is not missing, it is wearing its neighbour's
                # material.
                keepers = sorted(set(swaps) - collided - set(loose))
                print(f"      note: {row['name'] or bp_pkg}: "
                      f"{', '.join(sorted(collided))} shares one material "
                      f"with {', '.join(keepers) or 'another slot'} in this "
                      "model, so a pak cannot give them different looks -- "
                      "they follow the one that can")
            for slot in set(loose) | collided:
                r = swaps[slot][1][0].lower()
                if r in by_name:
                    dead_repl.add(by_name[r])
            stranded = sorted(set(loose) - collided)
            if stranded:
                print(f"      note: {row['name'] or bp_pkg}: "
                      f"{', '.join(stranded)} cannot swap in a pak "
                      "-- those parts keep their normal look")
            if not overrides and not repoint:
                continue

            keep = set()
            # Only what actually applies. A replacement for a slot that
            # cannot swap is dead weight in the pak -- and dead weight that
            # drags its whole material chain in behind it.
            repl_pids = {by_name[r[0].lower()]
                         for slot, (_b, r, _sh) in swaps.items()
                         if slot not in collided and slot not in loose
                         and r[0].lower() in by_name}
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
            keep |= {by_name[b.lower()] for b in carry}
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
                bases={mesh_pid}, costume_root=costume_root, carry=carry))
        if not matched:
            print(f"      note: {row['name'] or bp_pkg}: its material swap "
                  "has no effect a pak can carry -- Dresscode only")
    # Replacements no row could apply serve nothing in pak form. Without
    # this they fall out of every toggle's `keep` and land in the BASE pak
    # instead -- 19 materials the outfit never reads, carried into every
    # install for no reason.
    used = set().union(*(t["keep"] for t in toggles)) if toggles else set()
    dead = set()
    for pid in dead_repl:
        if pid not in used:
            dead |= closure(pid)
    dead -= used | base_union
    return toggles, blob_only, dead


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


def loose_root_of(target):
    """The /Game/ folder a plan's packages all sit under -- Player for a
    costume, Weapon for a weapons-menu row. The container mounts there, so
    every package must share it."""
    root = "/".join(target.split("/")[:4])
    return root + "/" if root.startswith("/Game/Character/") else LOOSE_ROOT


def loose_path_under(root):
    """Container-relative path of a package, under `root`'s mount."""
    def path(package_name):
        if not package_name.startswith(root):
            raise RuntimeError(f"cannot place {package_name} in a pak")
        return package_name[len(root):]
    return path

