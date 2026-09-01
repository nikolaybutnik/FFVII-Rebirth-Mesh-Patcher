"""
weapons.py -- convert weapon add-on paks into Dresscode WEAPON tiles.

Dresscode's weapons menu is fed by the same PDA_ModData_Character class as
costumes: its own container ships DA_ModData_CharacterDefaults (the stock
costume tiles) and DA_ModData_WeaponDefaults (the stock weapon tiles), and
the ONLY difference is a top-level "Mod Type" property -- a ByteProperty
holding E_ModType::NewEnumerator2. A mod's asset carrying that value
registers WEAPON rows; proven in game 2026-08-05 with a probe whose one row
equipped from the weapons menu.

A weapon add-on pak (a glove recolour riding a costume mod, say) overrides
stock files under /Game/Character/Weapon/ that the OUTFIT never references,
so it cannot ride an outfit tile -- and a plugin cannot ship /Game/ paths at
all. What it CAN do is become a weapon tile: carry a copy of the stock
weapon mesh into the plugin, plus private copies of just the stock materials
that sample what the pak overrides, plus the pak's own packages -- the same
graft-and-repoint dance stockgraft does for costume retouches. The row's
mesh path then points at the copy, and picking the tile equips the recoloured
weapon while the stock tile keeps the stock look.

Every carried package gets a tile-private name under /<Mod>/Weapons/<Safe>/,
so two tiles recolouring the same weapon differently never collide, and the
mesh itself lands at /<Mod>/Weapons/<Safe> with its export renamed to match.
That leaf is digit-free on purpose: stock weapon leaves like WE0002_15 are
FName-numbered, and keeping our own names out of that encoding entirely is
cheaper than being right about it everywhere.
"""

import glob
import os
import re
import struct

import cityhash
import conheader
import config
import iostore
import moddata
import pkgedit
import rename
import stockgraft
import stockslots
from zen import ZenPackage

WEAPON_ROOT = "/game/character/weapon/"
# The same root as the game spells it -- for building names, not matching.
WEAPON_ROOT_PROPER = "/Game/Character/Weapon/"

# The E_ModType value Dresscode's own DA_ModData_WeaponDefaults carries.
MOD_TYPE_WEAPON = "E_ModType::NewEnumerator2"

SKELETAL_MESH = cityhash.object_id("/Script/Engine", "SkeletalMesh", 1)


def is_weapon_pak(pkgs):
    """At least one stock weapon override and nothing touching another
    character tree -- author signature dummies (a lone /Game/X package)
    ride along in the wild and do not make a weapon pak an outfit."""
    weapon = other = False
    for p in pkgs.values():
        n = p["name"].lower()
        if n.startswith(WEAPON_ROOT) and n.count("/") >= 5:
            weapon = True
        elif n.startswith("/game/character/"):
            other = True
    return weapon and not other


def is_tile_path(mesh_package):
    """Whether a row's mesh is a tile THIS TOOL built: /<Plugin>/Weapons/
    <Safe>, with whatever it carries beneath it. An author's own weapon mod
    files its meshes under its plugin's own folders instead."""
    parts = mesh_package.split("/")
    return len(parts) == 4 and parts[2].lower() == "weapons"


def folder_of_tile(mesh_package):
    """The stock weapon a tile's own name records, or None.

    A tile that replaced the weapon MODEL outright carries no other file, so
    there is no tail to read the weapon off. Its imports are no help either:
    an all-weapons pak is routinely one mesh saved into every slot, and every
    copy still imports the FIRST weapon's skeleton -- reading that would
    override the wrong weapon. So the tile's own name carries the answer.
    """
    m = re.search(r"(WE\d{4}_\d{2}_[A-Za-z0-9_]+)$",
                  mesh_package.rsplit("/", 1)[-1])
    return m.group(1) if m else None


def weapon_folders(pkgs):
    """The stock weapons a pak covers -- one menu tile each. A single pak
    routinely does a character's whole set."""
    return {p["name"].split("/")[4] for p in pkgs.values()
            if p["name"].lower().startswith(WEAPON_ROOT)
            and p["name"].count("/") >= 5}


def weapon_name(folder):
    """The readable half of a stock weapon folder, for anything a person
    reads: WE0000_00_Cloud_BusterSword -> BusterSword."""
    bits = folder.split("_", 3)
    return bits[3] if len(bits) > 3 else folder


# Weapon numbers mirror the PC numbers, except where they do not: Sephiroth
# is PC0010 and his Masamune is WE1021. He is the only playable character
# the mirror misses, so the menu list below is what settles the rest.
WEAPON_NUMBERS = {"1021": "SEPHIROTH"}


def player_for(folder):
    """WE0002_15_Tifa_... -> TIFA, from the game's own menu list where it
    has the weapon and from the PC numbering otherwise."""
    num = folder[2:6]
    mirrored = None
    for key, (prefix, _folder) in moddata.PLAYER_TYPES.items():
        if prefix[2:6] == num:
            mirrored = key
    # One weapon can belong to two characters -- Zack and Cloud share the
    # Buster Sword -- so the mirror breaks the tie where it has an answer.
    menu = menu_weapons().get(folder.lower())
    if menu:
        return mirrored if mirrored in menu else sorted(menu)[0]
    return mirrored or WEAPON_NUMBERS.get(num)


_previews = None
_menu = None


def _read_dresscode():
    """Dresscode's own weapon list: the billboard per stock weapon, and
    which characters equip it. Empty when Dresscode is not readable here."""
    global _previews, _menu
    if _previews is not None:
        return
    _previews, _menu = {}, {}
    pat = os.path.join(getattr(config, "MODS_DIR", "") or "", "Dresscode",
                       "Content", "Paks", "WindowsNoEditor", "*.utoc")
    for u in glob.glob(pat):
        try:
            toc = iostore.Toc(u)
            for pid, p in rename.read_packages(toc).items():
                if not p["name"].endswith("DA_ModData_WeaponDefaults"):
                    continue
                for row in moddata.read_outfits(toc.read(p["chunk"])):
                    mesh = (row["skeletal_mesh"] or "").split(".")[0]
                    if not mesh:
                        continue
                    if row["preview_image"]:
                        _previews[mesh.lower()] = row["preview_image"]
                    if mesh.count("/") >= 4:
                        who = (row.get("player_type") or "").split("::")[-1]
                        if who:
                            _menu.setdefault(
                                mesh.split("/")[4].lower(), set()).add(who)
            toc.close()
        except Exception:
            pass


def menu_weapons():
    """{stock weapon folder (lower) -> {character}} for every weapon the
    game equips, read from the installed Dresscode. The game ships far more
    weapon folders than it equips -- cutscene variants, and the first
    game's weapons carried over unused -- and none of those belong in a
    menu. Empty when Dresscode is not readable: then nothing is filtered."""
    _read_dresscode()
    return _menu


def stock_preview(mesh_name):
    """The billboard Dresscode's own weapon list shows for this stock weapon,
    from the installed Dresscode -- None when that is not readable here."""
    _read_dresscode()
    return _previews.get(mesh_name.lower())


_stock_index = None


# End/Content/Character/Weapon/WE0000_00_Cloud_BusterSword/Model/WE0000_00
_STOCK_MESH_PATH = re.compile(
    r"Character/Weapon/(WE\d{4}_\d{2}[^/]*)/Model/(WE\d{4}_\d{2})\.uasset$",
    re.I)


def stock_index():
    """
    {mesh leaf (lower) -> stock weapon mesh package} for every weapon the
    game has.

    A character has one default costume but a dozen weapons, so unlike a
    costume the stock package a weapon mod stands in for cannot be worked
    out from its row alone -- but the files keep the weapon's own id
    (WE0000_00), and this turns that id back into a package.

    Read from the GAME's own container directory first: it lists every
    weapon (208 of them, against the 54 Dresscode's menu covers) and is
    there whenever the game is. Dresscode's weapon list is the fallback for
    a machine with the mod but not the game. When neither source can name
    the weapon, the conversion uses that character's default instead.
    """
    global _stock_index
    if _stock_index is not None:
        return _stock_index
    _stock_index = {}
    for u in stockgraft._utocs():
        try:
            for p in stockgraft._toc(u).paths.values():
                m = _STOCK_MESH_PATH.search(p.replace("\\", "/"))
                if m:
                    _stock_index[m.group(2).lower()] = (
                        f"{WEAPON_ROOT_PROPER}{m.group(1)}/Model/{m.group(2)}")
        except Exception:
            pass
    if not _stock_index:
        pat = os.path.join(getattr(config, "MODS_DIR", "") or "", "Dresscode",
                           "Content", "Paks", "WindowsNoEditor", "*.utoc")
        for u in glob.glob(pat):
            try:
                toc = iostore.Toc(u)
                for p in rename.read_packages(toc).values():
                    if not p["name"].endswith("DA_ModData_WeaponDefaults"):
                        continue
                    for row in moddata.read_outfits(toc.read(p["chunk"])):
                        mesh = (row["skeletal_mesh"] or "").split(".")[0]
                        if mesh.lower().startswith(WEAPON_ROOT):
                            _stock_index[mesh.rsplit("/", 1)[-1].lower()] = mesh
                toc.close()
            except Exception:
                pass
    return _stock_index


def stock_for(mesh_name, names):
    """
    The stock weapon package a mod's weapon mesh stands in for, or None.

    The mesh's own leaf usually IS the weapon id. Where an author renamed
    it (a second, physics-free copy of one sword), the materials and
    textures beside it still carry the id, so the longest id prefix any of
    them shares with a real weapon decides it.
    """
    index = stock_index()
    if not index:
        return None
    leaf = mesh_name.rsplit("/", 1)[-1].lower()
    if leaf in index:
        return index[leaf]
    for n in sorted(names, key=len, reverse=True):
        tail = n.rsplit("/", 1)[-1].lower()
        for k in index:
            if tail.startswith(k):
                return index[k]
    return None


def default_weapon_folder(player_type):
    """
    The stock weapon folder a character starts the game with.

    Zack has no WE0009 tree -- he uses Cloud's swords -- so his default
    is the Buster Sword. Everyone else is the first `_00` weapon whose
    folder names them.
    """
    key = player_type.split("::")[-1].upper()
    if key == "ZACK":
        key = "CLOUD"
    info = moddata.PLAYER_TYPES.get(key)
    if not info:
        return None
    tag = info[1].split("_")[2]
    for folder in stockslots.WEAPONS:
        bits = folder.split("_")
        if len(bits) >= 4 and bits[1] == "00" and bits[2].startswith(tag):
            return folder
    return None


def default_weapon_package(player_type):
    """
    The stock mesh package the character's default weapon lives in.

    Used when a Dresscode weapon row names no WE####_## -- authors often
    ship `/Mod/Assets/BatteringSword` and still mean the Buster Sword.
    """
    folder = default_weapon_folder(player_type)
    if not folder:
        return None
    slot = "_".join(folder.split("_")[:2])
    return f"{WEAPON_ROOT_PROPER}{folder}/Model/{slot}"


def _part_meta(toc, pkgs=None):
    """{pid: (exports, bundles, imported pids)} from a part pak's header --
    or counted off its packages when it has no readable one."""
    return conheader.store_meta(toc, pkgs if pkgs is not None
                                else rename.read_packages(toc))


def replaces_stock_mesh(tile_data, stock_name):
    """
    Whether a tile's mesh is the mod's OWN, rather than the stock weapon
    carried in so the menu has something to show.

    Only the export payload is compared: carrying the mesh rewrote its
    header (names, imports, the export's menu-safe name), but never a byte
    of the mesh itself. True when the game's copy cannot be read either --
    keeping a mesh that turns out to be stock costs size, dropping one that
    was the mod's loses the replacement.
    """
    pid = cityhash.package_id(stock_name)
    place = stockgraft._locate({pid})
    got = place.get(pid, {}).get("pkg")
    if not got:
        return True
    try:
        u, k = got
        stock = stockgraft._toc(u).read(k)
        return (tile_data[ZenPackage(tile_data).export_data_start():]
                != stock[ZenPackage(stock).export_data_start():])
    except Exception:
        return True


def _part_bulks(toc, pid):
    return [(bytes(toc.chunk_ids[i]), toc.read(i)) for i in range(toc.n)
            if toc.chunk_ids[i][11] in (3, 4)
            and int.from_bytes(toc.chunk_ids[i][:8], "little") == pid]


def build_tile(plugin, safe, parts, say=print, label=None):
    """
    Carry one weapon combination into the plugin -- recolours (stock mesh,
    repointed materials) and model replacements (the pak's own mesh) both.

    `parts` is [(toc, pkgs)] in load order (later wins a collision, like the
    ~mods rules the paks were written for). Returns (carried, rows): carried
    is {pid: rec} shaped like mkdc.build's merged entries; rows is one
    (row label, mesh soft path, player key, stock mesh package) per weapon
    touched.

    The game's own files are used where the PAK does not carry what a tile
    needs -- a recolour ships no weapon, so the stock mesh and the materials
    sampling its textures are copied in. A pak that replaces the model
    outright already carries everything, and builds with no game installed:
    that case is never made to depend on one.
    """
    overrides, metas = {}, {}
    for toc, pkgs in parts:
        pm = _part_meta(toc, pkgs)
        for pid, p in pkgs.items():
            n = p["name"].lower()
            if not (n.startswith(WEAPON_ROOT) and n.count("/") >= 5):
                say(f"      note: {p['name'].rsplit('/', 1)[-1]} is not "
                    "weapon content -- left out of the tile")
                continue
            overrides[pid] = (p, toc)
            metas[pid] = pm.get(pid, (1, 1, []))
    o_pids = set(overrides)
    if not o_pids:
        return {}, []

    folders = sorted({ov[0]["name"].split("/")[4]
                      for ov in overrides.values()})
    place = stockgraft._locate(
        {cityhash.package_id("/Game/Character/Weapon/"
                             f"{f}/Model/{'_'.join(f.split('_')[:2])}")
         for f in folders})

    carried, rows = {}, []
    for folder in folders:
        prefix = "_".join(folder.split("_")[:2])
        mesh_name = f"/Game/Character/Weapon/{folder}/Model/{prefix}"
        mesh_pid = cityhash.package_id(mesh_name)
        player = player_for(folder)
        pl = place.get(mesh_pid)
        # A pak replacing the weapon MODEL itself supplies the mesh; the
        # walk below then only rounds up whatever else of the pak's it uses.
        # Only the other kind -- a recolour, which ships no mesh -- has to
        # borrow one from the game.
        mod_mesh = mesh_pid in o_pids
        if not player:
            say(f"      note: {folder} is not a weapon this tool knows "
                "-- skipped")
            continue
        menu = menu_weapons()
        if menu and folder.lower() not in menu:
            say(f"      note: {weapon_name(folder)} is not a weapon the "
                "game equips -- no tile")
            continue
        if mod_mesh:
            ment = (metas[mesh_pid][0], metas[mesh_pid][1],
                    list(metas[mesh_pid][2]))
        else:
            if not pl or not pl["pkg"]:
                say(f"      note: {weapon_name(folder)} is a recolour, so "
                    "it needs the game installed to build on. Left out -- "
                    "keep that pak in ~mods.")
                continue
            ment = stockgraft._entry(mesh_pid, pl)
            if ment is None:
                continue

        # Walk outward from the mesh until everything sampling an overridden
        # package is found -- material instance chains put the texture two
        # hops out, so this mirrors stockgraft's bounded walk.
        frontier = list(ment[2])
        parent = {d: None for d in frontier}
        entries, hits, seen = {}, set(), set()
        for _depth in range(3):
            place.update(stockgraft._locate(
                {d for d in frontier if d not in place and d not in o_pids}))
            nxt = []
            for d in frontier:
                if d in seen:
                    continue
                seen.add(d)
                if d in o_pids:
                    hits.add(d)
                    continue
                dpl = place.get(d)
                if not dpl or not dpl["pkg"]:
                    continue
                ent = stockgraft._entry(d, dpl)
                if ent is None:
                    continue
                entries[d] = (dpl, ent)
                ddeps = set(ent[2])
                if ddeps & o_pids:
                    hits.add(d)
                else:
                    for dd in ddeps:
                        if dd not in seen and dd not in parent:
                            parent[dd] = d
                            nxt.append(dd)
            frontier = nxt
            if not frontier:
                break
        if not hits and not mod_mesh:
            say(f"      note: nothing on {weapon_name(folder)} uses what "
                "that pak changes -- skipped")
            continue

        # Chains back to the mesh, the overridden packages they sample, and
        # the mesh itself: the tile's private copy of the weapon.
        chain = set()
        for h in hits:
            p = h
            while p is not None and p not in chain:
                chain.add(p)
                p = parent.get(p)
        needed = set()
        for c in chain:
            if c in o_pids:
                needed.add(c)
            else:
                needed |= set(entries[c][1][2]) & o_pids

        # The walk above stops at the first package the PAK owns, because it
        # is looking for what the stock tree touches. A pak that replaces the
        # model brings its own chain -- mesh to materials to textures -- and
        # everything past that first material would be left behind, so the
        # tile loaded with the right shape and no textures on it at all
        # (field report: a grey sword). Follow the pak's own references to
        # the end.
        todo = list(needed)
        while todo:
            for d in metas.get(todo.pop(), (0, 0, []))[2]:
                if d in o_pids and d not in needed:
                    needed.add(d)
                    todo.append(d)

        # The weapon's own folder ends the name, which is what tells the
        # conversion back which weapon this tile stands in for -- a tile
        # replacing the model outright carries nothing else that says so.
        # It doubles as the thing keeping one pak's tiles apart.
        tsafe = f"{safe}_{folder}"
        root = f"/{plugin}/Weapons/{tsafe}"

        def fetch(pid):
            if pid in o_pids:
                p, ptoc = overrides[pid]
                exp, bun, deps = metas[pid]
                return (p["name"], ptoc.read(p["chunk"]), exp, bun,
                        list(deps), _part_bulks(ptoc, pid))
            dpl, (exp, bun, deps) = entries[pid]
            u, k = dpl["pkg"]
            t = stockgraft._toc(u)
            bulks = [(bytes(stockgraft._toc(bu).chunk_ids[bk]),
                      stockgraft._toc(bu).read(bk))
                     for bu, bk in dpl["bulks"]]
            return (pkgedit.package_name_of(ZenPackage(t.read(k))),
                    t.read(k), exp, bun, list(deps), bulks)

        if mod_mesh:
            p, ptoc = overrides[mesh_pid]
            mesh_data = ptoc.read(p["chunk"])
            mesh_bulks = _part_bulks(ptoc, mesh_pid)
        else:
            mesh_data = stockgraft._toc(pl["pkg"][0]).read(pl["pkg"][1])
            mesh_bulks = [(bytes(stockgraft._toc(bu).chunk_ids[bk]),
                           stockgraft._toc(bu).read(bk))
                          for bu, bk in pl["bulks"]]
        pool = {mesh_pid: (mesh_name, mesh_data, ment[0], ment[1],
                           list(ment[2]), mesh_bulks)}
        for pid in sorted((chain | needed) - {mesh_pid}):
            pool[pid] = fetch(pid)

        renames = {mesh_name.lower(): root}
        for pid, (name, *_rest) in pool.items():
            if pid != mesh_pid:
                renames[name.lower()] = \
                    root + "/" + name[len("/Game/Character/Weapon/"):]

        mesh_pkg = ZenPackage(mesh_data)
        obj = next((e["name"] for e in mesh_pkg.exports
                    if e["cls"] == SKELETAL_MESH), mesh_pkg.exports[0]["name"])
        object_renames = {mesh_name.lower(): {obj: tsafe}}

        pseudo = {pid: dict(name=name,
                            exports=[pkgedit.export_object_path(
                                ZenPackage(data), e)
                                for e in ZenPackage(data).exports])
                  for pid, (name, data, *_r) in pool.items()}
        pkgid_map, import_map, string_map = rename.build_maps(
            pseudo, renames, object_renames)

        for pid, (name, data, exp, bun, deps, bulks) in pool.items():
            pkg = ZenPackage(data)
            names = [rename.map_path(n, renames, string_map)
                     for n in pkg.names]
            for mapped in (pkg.name, pkg.srcname):
                idx, number = mapped & 0x3FFFFFFF, mapped >> 32
                if number and idx < len(names):
                    resolved = pkg.name_at(mapped & 0xFFFFFFFF, number)
                    moved = renames.get(resolved.lower())
                    if moved:
                        names[idx] = pkgedit.split_name_number(moved)[0]
            source = pkgedit.source_name_of(pkg)
            exports = {}
            if pid == mesh_pid:
                exports = {e["idx"]: tsafe for e in pkg.exports
                           if e["name"] == obj}
            new_pid = cityhash.package_id(renames[name.lower()])
            carried[new_pid] = dict(
                name=renames[name.lower()],
                data=pkgedit.rewrite(
                    data, names=names, import_map=import_map,
                    pkgid_map=pkgid_map,
                    new_package_name=renames[name.lower()],
                    new_source_name=renames.get(source.lower(), source),
                    export_names=exports),
                deps=[pkgid_map.get(d, d) for d in deps],
                exp=exp, bun=bun,
                bulks=[(new_pid.to_bytes(8, "little") + bytes(bid[8:]), bd)
                       for bid, bd in bulks])

        # One pak can cover several weapons (an invisible-weapons pak does
        # all twelve); each gets its own row, told apart by the weapon's
        # own name.
        row_label = label or tsafe
        if len(folders) > 1:
            row_label = f"{row_label} - {weapon_name(folder)}"
        extra = len(pool) - 1
        say(f"      weapon tile  {row_label}: rides the WEAPONS menu"
            + (f" (+{extra} supporting file{'s' if extra != 1 else ''})"
               if extra else ""))
        rows.append((row_label, f"{root}.{tsafe}", player, mesh_name))

    return carried, rows
