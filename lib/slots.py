"""
slots.py -- which stock costume or weapon a pak stands in for, and moving it
onto different ones.

A ~mods pak wins by OVERRIDING: its packages carry the same /Game/ paths as
the game's own, so the loader finds the mod's copy first. Which costume you
see it on is therefore decided by nothing but the paths inside it --
/Game/Character/Player/PC0002_00_Tifa_Standard/... is Tifa's default outfit
because that is where the game keeps it.

Repointing is renaming those paths onto another stock folder. rename.py
already knows how to move a package; this module knows WHERE the game's
costumes and weapons live, so it can say what a pak currently replaces and
offer the alternatives.

WHY ONLY THE SAME CHARACTER
---------------------------
Because a character's costumes all hang off ONE skeleton. Checked against
the game's own files: every one of Tifa's eight costume meshes imports
PC0002_00_Tifa_Standard/Model/PC0002_00_Skeleton -- the default costume's,
wherever the outfit itself lives. So a mesh built for one of her slots is
built for all of them, and moving it between them is sound. Move it to
another character and it is driven by a skeleton it was never rigged to,
which is why that is not on the menu.

ONE SOURCE, SEVERAL TARGETS
---------------------------
Authors routinely cook one outfit over every slot a character has, so it
follows them through story scenes that force a costume change. That is a
copy of the MESH per slot -- everything else (materials, textures) stays in
one place and every copy imports it from there, because imports resolve by
ID and all the copies ride in the same container. Duplicating the lot would
multiply a 300MB mod by ten for no gain.
"""

import re

import cityhash
import conheader
import loosepak
import rename
import stockgraft
import stocknames
import stockslots

COSTUME = "costume"
WEAPON = "weapon"

ROOTS = {
    COSTUME: "/Game/Character/Player/",
    WEAPON: "/Game/Character/Weapon/",
}

# A stock folder is <id>_<Character>_<Name>, the id being PC0002_00 or
# WE0002_15 -- two letters, four digits, an underscore and two more. Case
# insensitive because a package ID hashes the path lowercased, so a mod can
# and sometimes does spell a stock path in its own case and still override.
SLOT_ID = {
    COSTUME: re.compile(r"^PC\d{4}_\d{2}(?=_|$)", re.I),
    WEAPON: re.compile(r"^WE\d{4}_\d{2}(?=_|$)", re.I),
}

# Where the per-slot files live inside a folder: the mesh, and the physics,
# condition and effect assets named after it. These are the ones a second
# slot needs its own copy of.
MODEL_DIR = "model"

_stock = {}
_stock_lower = {}


def _mesh_pattern(kind):
    root = ROOTS[kind][len("/Game/"):]
    letters = "PC" if kind == COSTUME else "WE"
    return re.compile(
        root + r"(" + letters + r"\d{4}_\d{2}[^/]*)/Model/"
        r"(" + letters + r"\d{4}_\d{2})\.uasset$", re.I)


def stock(kind):
    """
    {stock folder -> its mesh leaf} for every costume (or weapon) the game
    has a model for.

    Read from the game containers' own directory index when the game is
    here -- no container is decompressed, and it lists every slot rather
    than the handful any one mod menu covers. Folders with no mesh (a
    texture-only story variant) are left out: a pak moved onto one would
    override nothing.

    With no game installed it comes from stockslots.py, a copy of the same
    list. That list is identical on every install, so there is no reason to
    demand the game just to show a menu -- but the live read still wins
    where it is available, so a game update that adds a costume works
    before the copy catches up.
    """
    got = _stock.get(kind)
    if got is not None:
        return got
    found, pattern = {}, _mesh_pattern(kind)
    for u in stockgraft._utocs():
        try:
            toc = stockgraft._toc(u)
        except Exception:
            continue
        for p in toc.paths.values():
            m = pattern.search(p.replace("\\", "/"))
            if m:
                found[m.group(1)] = m.group(2)
    if not found:
        # The mesh leaf is always the folder's own slot id -- checked over
        # all 85 costumes and 208 weapons -- so the copy stores names only.
        names = (stockslots.COSTUMES if kind == COSTUME
                 else stockslots.WEAPONS)
        found = {f: SLOT_ID[kind].match(f).group(0) for f in names}
    _stock[kind] = found
    _stock_lower[kind] = {f.lower() for f in found}
    return found


def have_game():
    """Whether the game's own containers are readable here. Everything works
    without them except naming the replacements that will stop applying,
    which is a question only the game's files can answer."""
    return bool(stockgraft._utocs())


def is_stock(kind, folder):
    """Whether the game has a costume (or weapon) by that folder name. Case
    insensitive -- a repacked mod does not always keep the game's."""
    stock(kind)
    return folder.lower() in _stock_lower[kind]


def slot_id(kind, folder):
    """PC0002_05_Tifa_Soldier -> PC0002_05, or None if that is not one."""
    m = SLOT_ID[kind].match(folder)
    return m.group(0) if m else None


def character_of(kind, folder):
    """The character number a slot belongs to: PC0002_05_... -> PC0002."""
    sid = slot_id(kind, folder)
    return sid[:6] if sid else None


def _spaced(text):
    """A run of joined words as words: SoldierNoHelmet -> Soldier No Helmet,
    RedXIII -> Red XIII, CostaClothing2 -> Costa Clothing 2."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)


def label(folder):
    """
    What to call a stock slot in front of a person:
    PC0002_05_Tifa_Soldier -> Soldier, WE0000_02_Cloud_HardBreaker ->
    Hardedge.

    A weapon folder is named after the MODEL, which is often not what the
    game calls the weapon -- and six of each character's twelve are filed as
    END2Weapon1..6, which is no name at all. stocknames.py has the real ones,
    read out of the game's own text, so it wins wherever it has an entry.

    Costumes are not in that table: the game names only the handful its
    outfit menu offers, and "Ex-SOLDIER: First Class" tells someone picking a
    slot less than "Standard" does. Their folder tail, spaced into words, is
    the best there is -- and it is also what an enemy's weapon falls back to.
    """
    sid = SLOT_ID[WEAPON].match(folder)
    named = stocknames.WEAPONS.get(sid.group(0).upper()) if sid else None
    if named:
        return named
    bits = folder.split("_", 3)
    return _spaced(bits[3]) if len(bits) > 3 else folder


def character_name(folder):
    """PC0002_05_Tifa_Soldier -> Tifa, PC0004_00_RedXIII_... -> Red XIII."""
    bits = folder.split("_", 3)
    return _spaced(bits[2]) if len(bits) > 2 else ""


def choices_for(kind, folder):
    """
    The stock slots a pak on `folder` can be moved to: the same character's,
    in slot order.

    Only the same character. Another one's costume is built on another
    skeleton, so it would load and then animate as a heap -- offering it
    would only be offering a way to break the game.
    """
    char = (character_of(kind, folder) or "").lower()
    if not char:
        return []
    return sorted(f for f in stock(kind)
                  if (character_of(kind, f) or "").lower() == char)


# ---------------------------------------------------------------------------
# What a pak points at
# ---------------------------------------------------------------------------

def folder_of(kind, name):
    """
    The stock slot a package name sits in, or None.

    It has to be a slot the GAME has, not merely something shaped like one.
    Mods invent folders in the same tree -- PC0003_99_Skin, holding skin
    textures several outfits share -- and those are named for a slot they
    are not: moving one onto a costume would take a shared retouch off
    every other outfit that relied on it, silently. Anything the game does
    not have a costume at is left exactly where the author put it.
    """
    root = ROOTS[kind]
    if not name.lower().startswith(root.lower()):
        return None
    rest = name[len(root):]
    if "/" not in rest:
        return None
    folder = rest.split("/")[0]
    return folder if is_stock(kind, folder) else None


def survey(packages):
    """
    What a pak replaces, as (kind, {stock folder -> [package IDs]}, others).

    `others` are packages outside the character trees -- an author's
    signature dummy, a shared skin retouch, a common detail texture. They
    ride along untouched, because they are not part of what the slot means.

    kind is None when the pak is neither a costume nor a weapon, or is both;
    the caller reports that rather than guessing.
    """
    hits = {COSTUME: {}, WEAPON: {}}
    others = []
    for pid, p in packages.items():
        for kind in (COSTUME, WEAPON):
            folder = folder_of(kind, p["name"])
            if folder:
                hits[kind].setdefault(folder, []).append(pid)
                break
        else:
            others.append(pid)
    if hits[COSTUME] and hits[WEAPON]:
        return None, {}, others
    for kind in (COSTUME, WEAPON):
        if hits[kind]:
            return kind, hits[kind], others
    return None, {}, others


# ---------------------------------------------------------------------------
# Moving it
# ---------------------------------------------------------------------------

def moved_name(kind, name, src, dst):
    """
    `name` as it would be called on stock folder `dst`.

    The folder changes, and so does every slot id inside it: the mesh at
    PC0002_00_Tifa_Standard/Model/PC0002_00 becomes
    PC0002_05_Tifa_Soldier/Model/PC0002_05. The game asks for a slot's mesh
    by the slot's own id, so leaving the leaf alone would leave the pak
    overriding nothing.
    """
    root = ROOTS[kind]
    tail = name[len(root) + len(src):]
    return root + dst + re.sub(re.escape(slot_id(kind, src)),
                               slot_id(kind, dst), tail, flags=re.I)


def _export_rename(old, new):
    """{old export name -> new one} when a package's leaf changed.

    An import names an OBJECT, not a package, and its ID hashes both halves.
    A mesh moved onto PC0002_05 whose object is still called PC0002_00
    answers to an ID nothing asks for -- which looks exactly like the mod
    not being installed.
    """
    old_leaf, new_leaf = old.rsplit("/", 1)[-1], new.rsplit("/", 1)[-1]
    return {old_leaf: new_leaf} if old_leaf != new_leaf else None


def per_slot_packages(kind, packages, src):
    """
    The packages a second slot needs its own copy of: what sits in the
    source folder's Model/ and carries the slot's id.

    That is the mesh plus the physics, condition and effect assets beside
    it -- the files the game looks up by the slot's own path. Materials and
    textures are not among them: they are reached through the mesh, so one
    copy serves every slot.
    """
    sid = slot_id(kind, src).lower()
    out = []
    for pid, p in packages.items():
        if folder_of(kind, p["name"]) != src:
            continue
        rest = p["name"][len(ROOTS[kind]) + len(src) + 1:].split("/")
        if len(rest) == 2 and rest[0].lower() == MODEL_DIR \
                and rest[1].lower().startswith(sid):
            out.append(pid)
    return out


def _bulks(toc, pid, new_pid):
    """A package's bulk chunks, re-stamped with its new package ID. Bulk data
    carries no paths of its own -- only the ID binding it to its .uasset --
    so it is copied compressed, never unpacked."""
    return [loosepak.copied(toc, i, new_pid)
            for i in range(toc.n)
            if toc.chunk_ids[i][11] in (3, 4)
            and int.from_bytes(toc.chunk_ids[i][:8], "little") == pid]


def lost_overrides(moved):
    """
    Of the packages being moved, the ones that overrode a game file and will
    no longer override anything. `moved` is [(old name, new name)].

    A costume slot does not have the same files as every other -- only the
    default has a skeleton, plenty have no physics asset. Moving such an
    override onto a slot the game has no counterpart in is not an error, and
    the mod still loads: that one part simply stops applying, silently. So
    it is named instead.
    """
    if not stockgraft._utocs():
        return []
    wanted = set()
    for old, new in moved:
        wanted.add(cityhash.package_id(old))
        wanted.add(cityhash.package_id(new))
    place = stockgraft._locate(wanted)

    def real(name):
        return bool(place.get(cityhash.package_id(name), {}).get("pkg"))

    return sorted({old for old, new in moved
                   if old != new and real(old) and not real(new)})


def repoint(toc, packages, kind, choices, out_dir, base, say=print,
            container_name=None):
    """
    Write a copy of this pak aimed at different stock slots.

        choices   {stock folder it replaces now -> [stock folders to replace]}

    The first target in each list is where the outfit's shared files land;
    the rest get a copy of its per-slot files. Returns the .utoc path.
    """
    meta = conheader.store_meta(toc, packages)

    # Only packages the PAK carries are renamed. A mesh also refers to stock
    # files in its old slot's folder -- its skeleton, its petrify variant --
    # and those references are left pointing where they point. Swapping them
    # to the new slot is tempting and wrong twice over: half the slots have
    # no counterpart (only the default costume has a skeleton, so the mesh
    # would lose it), and the mod's materials were cooked against the OLD
    # slot's, so the old slot's variants are the ones that match them.
    renames, objects, moved = {}, {}, []
    for pid, p in packages.items():
        src = folder_of(kind, p["name"])
        if src not in choices:
            continue
        new = moved_name(kind, p["name"], src, choices[src][0])
        renames[p["name"].lower()] = new
        moved.append((p["name"], new))
        exports = _export_rename(p["name"], new)
        if exports:
            objects[p["name"].lower()] = exports

    maps = rename.build_maps(packages, renames, objects)
    pkgid_map = maps[0]

    order, records = [], {}

    def keep(new_pid, name, chunk, exp, bun, deps, bulks):
        if new_pid in records:
            say(f"      note: two of your choices both want {name} -- "
                "keeping the first")
            return
        records[new_pid] = dict(chunk, name=name, exp=exp, bun=bun,
                                deps=deps, bulks=bulks)
        order.append(new_pid)

    def rewritten(pid, name, chunk_renames, chunk_maps, chunk_objects,
                  new_pid):
        """This package's new bytes -- or its original compressed blocks
        where the rewrite changed nothing, which is most of a pak that
        only touched one folder."""
        data = toc.read(packages[pid]["chunk"])
        out = rename.rewrite_package(data, name, chunk_renames, chunk_maps,
                                     chunk_objects, fix_arcs=True)
        if out == data and new_pid == pid:
            return loosepak.copied(toc, packages[pid]["chunk"], new_pid)
        return dict(data=out)

    for pid, p in packages.items():
        exp, bun, deps = meta.get(pid, (1, 1, []))
        new_pid = pkgid_map.get(pid, pid)
        keep(new_pid, renames.get(p["name"].lower(), p["name"]),
             rewritten(pid, p["name"], renames, maps,
                       objects.get(p["name"].lower()), new_pid),
             exp, bun, [pkgid_map.get(d, d) for d in deps],
             _bulks(toc, pid, new_pid))

    # Extra slots: the same per-slot files again, under the other slot's
    # name. Their own rename map differs from the shared one in exactly
    # those entries, so everything else they import still resolves to the
    # single copy written above.
    for src, targets in choices.items():
        slot_pids = per_slot_packages(kind, packages, src)
        if len(targets) > 1 and not slot_pids:
            # Nothing to copy, and nothing missing either: this pak's files
            # are reached THROUGH a mesh, and every copy of that mesh --
            # wherever it lives -- imports them from the one place they sit.
            say("      note: no mesh of its own, so it just rides along "
                "with whichever pak has one")
        for dst in targets[1:]:
            if not slot_pids:
                continue
            dup_renames, dup_objects = dict(renames), dict(objects)
            for pid in slot_pids:
                name = packages[pid]["name"]
                new = moved_name(kind, name, src, dst)
                dup_renames[name.lower()] = new
                moved.append((name, new))
                exports = _export_rename(name, new)
                dup_objects.pop(name.lower(), None)
                if exports:
                    dup_objects[name.lower()] = exports
            dup_maps = rename.build_maps(packages, dup_renames, dup_objects)
            dup_pkgids = dup_maps[0]
            for pid in slot_pids:
                name = packages[pid]["name"]
                exp, bun, deps = meta.get(pid, (1, 1, []))
                new_pid = dup_pkgids.get(pid, pid)
                keep(new_pid, dup_renames[name.lower()],
                     rewritten(pid, name, dup_renames, dup_maps,
                               dup_objects.get(name.lower()), new_pid),
                     exp, bun, [dup_pkgids.get(d, d) for d in deps],
                     _bulks(toc, pid, new_pid))

    lost = lost_overrides(moved)
    if lost:
        many = len(lost) != 1
        where = "one of the new slots" if any(len(t) > 1
                                              for t in choices.values()) \
            else "the new slot"
        say(f"      note: {len(lost)} game file{'s' if many else ''} this "
            f"pak replaced {'have' if many else 'has'} no counterpart on "
            f"{where}, so that part stops applying:")
        for name in lost[:3]:
            say(f"        {name.rsplit('/', 1)[-1]}")
        if len(lost) > 3:
            say(f"        and {len(lost) - 3} more")

    return loosepak.write(order, records, out_dir, base, toc,
                          container_name=container_name)
