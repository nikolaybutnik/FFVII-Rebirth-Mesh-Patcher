"""
template.py -- dresscode.json, the file a pak mod is converted through.

A Dresscode mod needs things a pak simply does not have -- a display
name, an author, per-outfit names, optional preview images -- and prompting
for each in a console is miserable. So the first drop of a pak mod writes
a dresscode.json template beside its paks, prefilled with everything
detectable, and the second drop reads it and builds. Editing it is optional:
the prefilled values already work.

The folder layout IS the variant structure: paks directly in the dropped
folder are a single outfit; each sub-folder holding paks is one variant,
named for its folder.
"""
import hashlib
import json
import os

import drops
import iostore
import rename
import slots

from formats import mkdc
from formats import weapons

TEMPLATE = "dresscode.json"
IMAGE_EXTS = (".png", ".jpg", ".jpeg")

# Folder names that mean "each subfolder here is a whole costume", as opposed
# to Optional, which means "each pak here changes materials on the costume".
VARIANT_DIRS = ("variants", "variant")


def _pak_digest(utoc):
    """A pak's bytes, .utoc and .ucas together."""
    h = hashlib.md5()
    for ext in (".utoc", ".ucas"):
        try:
            with open(os.path.splitext(utoc)[0] + ext, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
        except OSError:
            pass
    return h.digest()


def dedupe_paks(utocs):
    """
    One entry per DISTINCT pak. A mod that offers the same add-on to every
    costume ships a copy of it in each costume's folder, and listing it once
    per copy makes the template unreadable -- worse, entries name a pak by
    its stem, so repeated names are ambiguous.

    Bytes decide, not names: same-named paks that really differ all stay.
    Only a name-and-size collision is hashed, so nothing is read twice over
    for a mod whose paks are all distinct anyway.
    """
    groups = {}
    for u in utocs:
        stem = os.path.splitext(os.path.basename(u))[0].lower()
        try:
            size = os.path.getsize(os.path.splitext(u)[0] + ".ucas")
        except OSError:
            size = -1
        groups.setdefault((stem, size), []).append(u)
    out = []
    for group in groups.values():
        if len(group) == 1:
            out += group
            continue
        seen = set()
        for u in sorted(group):
            digest = _pak_digest(u)
            if digest not in seen:
                seen.add(digest)
                out.append(u)
    return sorted(out)


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


def stem_of(utoc):
    """A pak's name without its extension -- how every entry names it."""
    return os.path.splitext(os.path.basename(utoc))[0]


def menu_tiles_in(utocs):
    """The weapons a pak set puts in the menu -- as a SET, since paks in one
    entry routinely cover the same weapon, and minus the ones the game does
    not equip, which get no tile."""
    menu = weapons.menu_weapons()
    got = set().union(*(weapon_tiles_in(u) for u in utocs)) if utocs else set()
    return {f for f in got if not menu or f.lower() in menu}


def weapon_entries(extras):
    """
    [(pak, [pak, ...])] -- one weapon-menu entry per tile worth making.

    An entry covered by another is dropped: a pak that only feeds a second
    one is carried by it rather than offered as a tile of its own, which it
    could not render anyway.
    """
    need = weapons.part_needs(extras)
    out, seen = [], set()
    for u in extras:
        mine = set(need[u])
        if any(mine < set(need[v]) for v in extras):
            continue                    # another entry already carries it
        key = frozenset(mine)
        if key in seen:
            continue                    # two paks that need each other
        seen.add(key)
        out.append((u, need[u]))
    return out


def weapon_tiles_in(utoc):
    """The stock weapons a pak covers -- one menu tile each. Empty when the
    pak is not a weapon pak at all."""
    try:
        toc = iostore.Toc(utoc)
    except Exception:
        return set()
    try:
        pkgs = rename.read_packages(toc)
        return weapons.weapon_folders(pkgs) \
            if weapons.is_weapon_pak(pkgs) else set()
    except Exception:
        return set()
    finally:
        toc.close()


def is_weapon_pak(utoc):
    """Whether a pak replaces weapon content and nothing else."""
    return bool(weapon_tiles_in(utoc))


def mod_root_name(source):
    """The dropped folder's name, without the suffix a conversion added --
    a folder converted before the suffix was renamed keeps its name."""
    name = os.path.basename(os.path.abspath(source).rstrip("\\/"))
    for tag in (" (pak)", " (unpacked)"):
        if name.lower().endswith(tag):
            return name[:-len(tag)]
    return name


def loose_layout(source, mods):
    """
    (mod_name, [(relative folder, utoc)], [extra utoc], [companion utoc],
    {variant folders}) when `source` is the root of a pak mod in a
    shape the template supports -- else None.

    Outfits are the paks carrying a costume. Everything else under an
    Optional folder is an extra (a variant, opt-in); everything else
    ELSEWHERE is a companion -- a REQUIRED pak the outfit cannot render
    without (authors routinely ship the mesh in one pak and its materials
    and textures in another). Companions merge into every outfit.

    A VARIANTS folder is the third kind, and the one Optional cannot
    express: each subfolder holds a whole costume -- its own mesh -- so it
    becomes another outfit tile in the SAME Dresscode mod rather than a
    toggle. Optional swaps materials on the worn model, which is why a
    difference living inside the mesh (a part switched off, a reshaped
    body) has no Optional form at all.
    """
    if not mods or any(uplugin for _utoc, uplugin in mods):
        return None
    root = os.path.normcase(os.path.abspath(source))
    outfits, extras, companions, variants = [], [], [], set()
    for k, (utoc, _) in enumerate(mods):
        drops.scanning("reading paks", k, len(mods))
        folders = os.path.relpath(utoc, source).lower().split(os.sep)[:-1]
        # An Optional folder wins over content: a generated Optional tree's
        # paks DO carry their outfit's mesh, and stay extras.
        if "optional" in folders:
            extras.append(utoc)
        elif carries_outfit(utoc):
            outfits.append(utoc)
            if any(f in VARIANT_DIRS for f in folders):
                variants.add(utoc)
        else:
            companions.append(utoc)
    drops.scanning("reading paks", len(mods), len(mods))
    if not outfits:
        # A weapon mod has no costume to anchor on, and needs none: its rows
        # stand alone in the WEAPONS menu. Every pak has to be a weapon one,
        # or this is a costume mod whose main pak was left behind.
        rest = extras + companions
        if rest and all(is_weapon_pak(u) for u in rest):
            return (mod_root_name(source), [], dedupe_paks(rest), [], set())
        return None

    # The "one outfit per folder" rules below are about telling several
    # SEPARATE costumes apart. Variants are declared, not guessed, so they
    # are exempt: the main costume at the root plus a Variants tree is the
    # shape this folder is for.
    plain = [u for u in outfits if u not in variants]
    parts, by_folder = [], {}
    for utoc in outfits:
        d = os.path.dirname(os.path.abspath(utoc))
        by_folder.setdefault(os.path.normcase(d), []).append(utoc)
    for utoc in outfits:
        d = os.path.dirname(os.path.abspath(utoc))
        at_root = os.path.normcase(d) == root
        if at_root and len(plain) > 1:
            return None                 # several outfits loose in one folder
        # Any depth goes -- downloads nest however their author unpacked
        # them. Several outfits sharing one folder are named for their paks
        # ALONE, exactly as before this understood nesting: existing
        # dresscode.json files key their outfits by those stems.
        rel = os.path.relpath(d, source).replace("\\", "/")
        if len(by_folder[os.path.normcase(d)]) > 1:
            parts.append((os.path.splitext(os.path.basename(utoc))[0], utoc))
        else:
            parts.append(("." if at_root else rel, utoc))
    if len({p == "." for p, u in parts if u not in variants}) > 1:
        return None                     # a pak both at the root and in subs
    # Companions are NOT deduped: each is scoped to the outfit it sits with,
    # so an identical copy in a sibling folder is that outfit's own.
    return (mod_root_name(source), sorted(parts), dedupe_paks(extras),
            sorted(companions),
            {rel for rel, u in parts if u in variants})


# Folders that say where a pak is filed, never what the costume is: a
# download unpacked with its install path intact ends in one of these, and
# naming a menu row "~mods" tells nobody anything.
PACKAGING_DIRS = {"~mods", "mods", "content", "paks", "windowsnoeditor"}


def outfit_label(rel, mod_name):
    """What an outfit folder is called in the menu before anyone renames it.
    A variant is named for its own folder, not the path to it -- "No Jacket"
    reads as a costume, "Variants/No Jacket" reads as a filing system."""
    if rel == ".":
        return mod_name
    parts = rel.split("/")
    while len(parts) > 1 and parts[-1].lower() in PACKAGING_DIRS:
        parts.pop()
    return parts[-1] if parts[0].lower() in VARIANT_DIRS else "/".join(parts)


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


def pak_image(utoc):
    """The picture an add-on PAK nominates: one of the same name, beside it.
    No alphabetical fallback like find_image -- add-on paks share a folder,
    and the mod's own icon.png sits in one."""
    folder = os.path.dirname(utoc) or "."
    want = os.path.splitext(os.path.basename(utoc))[0].lower()
    for p in images_in(folder):
        if os.path.splitext(p)[0].lower() == want:
            return os.path.join(folder, p)
    return None


def write_template(source, mod_name, parts, prefill=None, restore=None,
                   extras=(), costume=None, sidecar=None):
    """Prefill dresscode.json with the little a build needs. Pictures are on
    purpose NOT in here -- they are picked up by where they sit.

    `prefill` carries real values when the pak mod was itself converted
    from a Dresscode mod; `restore` is that conversion's opaque record of the
    original, which makes converting back exact. `extras` is [(label, pak
    stem)] -- or (label, stem, is_weapon) triples -- for the mod's optional
    paks. Weapon paks get a ready-made variants entry (their tile stands
    alone in the weapons menu, there is nothing to combine); the rest are
    listed for the person to compose."""
    prefill = prefill or {}
    pre_outfits = prefill.get("outfits", {})
    outfits = [{"folder": rel,
                "name": pre_outfits.get(rel, (None,))[0]
                or outfit_label(rel, mod_name),
                "description": pre_outfits.get(rel, (None, ""))[1]}
               for rel, _utoc in parts]
    intro = [
        "convert.py made this file. Drop the folder on it again and it",
        "builds the Dresscode mod. Changing anything below is optional:",
        "the 'name' lines are what things are called in game, 'author'",
        "and the descriptions show on the mod's page.",
        "",
    ]
    if outfits:
        intro += [
            "Pictures: icon.png next to this file becomes the mod's",
            "thumbnail; preview.png inside an outfit's folder becomes that",
            "outfit's tile picture. An add-on pak's picture is a .png",
            "named after the pak, beside the pak.",
            "",
            "Leave the 'folder' lines alone -- they say where the files",
            "live.",
        ]
    else:
        # A weapon mod has no outfit folders and no tile pictures of its
        # own: each tile shows the game's picture of the weapon it replaces.
        intro += [
            "This mod changes weapons, not costumes, so 'outfits' is empty",
            "and stays that way.",
            "",
            "Pictures: icon.png next to this file becomes the mod's",
            "thumbnail. A weapon tile shows the game's picture of the",
            "weapon it replaces -- to change that, put a .png named after",
            "the pak beside the pak.",
        ]
    template = {
        "_how_this_works": intro,
        "name": prefill.get("name") or mod_name,
        "author": prefill.get("author", ""),
        "description": prefill.get("description", ""),
        "category": prefill.get("category") or "Outfit",
        "version": prefill.get("version") or "1.0.0",
        "stackable": False,
        "outfits": outfits,
    }
    extras = [tuple(e) + (None,) * (4 - len(e)) for e in extras]
    extras = [(l, s, w, p or [s]) for l, s, w, p in extras]
    if extras and restore:
        # Reproducing a real mod: its tiles are its own, exactly as they
        # were, or converting back would not give the same mod.
        template["_how_this_works"] += [
            "",
            "'variants' below IS the menu -- one entry = one tile, exactly",
            "as the original mod had them.",
        ]
        template["variants"] = [{"name": label, "parts": [stem]}
                                for label, stem, _w, _p in extras]
    elif extras:
        # A modular pak mod. One tile per outfit part would mean thirty
        # tiles that each change one thing, so those are listed and the
        # choosing is left to the person; a weapon part has nothing to
        # combine with, so its entry is written ready to build. The entry
        # shape rides as a REAL object ('_example_variant', built from the
        # mod's own parts) -- quotes in the help text would be escaped in
        # the raw file and copy out wrong.
        combos = [(l, s) for l, s, w, _p in extras if not w]
        weap = [(l, p) for l, _s, w, p in extras if w]
        if combos:
            template["_how_this_works"] += [
                "",
                "'variants' below is this mod's menu: one entry = one tile,",
                "and an entry's 'name' is the tile's label. Add a",
                "'description' line to one for a second line under it.",
                "'_example_variant' shows the shape, using this mod's own",
                "parts -- copy it into the 'variants' list and edit it.",
            ]
        if combos:
            template["_how_this_works"] += [
                "",
                "The paks in 'parts_you_can_combine' are NOT in the menu",
                "yet. Make entries for the combinations you actually wear,",
                "parts worn together in the same entry.",
                "",
                "Rather keep them as drag-in files? Set 'stackable' to true",
                "and you get a 'Put in ~mods' folder that works like",
                "always, with only the costume in the menu.",
            ]
            if len(parts) > 1:
                template["_how_this_works"] += [
                    "",
                    "An entry is offered on every outfit. For one that",
                    "only suits some, add an 'outfit' line to it naming",
                    "the outfit, spelled as in 'outfits' above -- or a",
                    "list of names.",
                ]
        if weap and outfits:
            template["_how_this_works"] += [
                "",
                "The WEAPON paks have their entries already -- each is one",
                "tile in Dresscode's WEAPONS menu, and the stock weapon",
                "tile switches it off. Nothing to do for those. That menu",
                "is separate: a weapon tile stays on whatever costume the",
                "character is wearing.",
            ]
        elif weap:
            template["_how_this_works"] += [
                "",
                "'variants' below is the menu, already written for you:",
                "one tile per weapon, in Dresscode's WEAPONS menu. Pick",
                "the stock weapon there to switch one off. Rename the",
                "entries if you like, then drop the folder again to build.",
            ]
        # Only where there is something to compose. Weapon paks are already
        # tiles, so an example built from them would be a trap: copied in as
        # it stands, it would register the same weapon a second time.
        if combos:
            ex = combos[:2]
            template["_example_variant"] = {
                "name": " + ".join(l for l, _s in ex),
                "parts": [s for _l, s in ex]}
            template["parts_you_can_combine"] = [s for _l, s in combos]
        template["variants"] = [{"name": l, "parts": p} for l, p in weap]
    if costume:
        template["_how_this_works"] += [
            "",
            f"These paks recolour {slots.costume_name(costume)}, named in",
            "'costume' below.",
        ]
        template["costume"] = costume
    if restore:
        template["_how_this_works"] += [
            "",
            "'restore' below is the original Dresscode mod, recorded so",
            "that converting back reproduces it exactly. Leave it alone.",
        ]
        if sidecar:
            template["_how_this_works"] += [
                f"Keep {sidecar} here too -- the rest of the original.",
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
    # No outfit FOLDERS means a weapon-only mod, where an empty list is the
    # right answer -- only a mod that has costume folders is missing something.
    if not outfits and by_folder:
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
        costume=data.get("costume"),
    )
    return meta, outfits


def plugin_id(name):
    """
    The display name reduced to a plugin identifier.

    Dresscode looks a mod up by its folder name and ignores it unless that
    exactly equals the .uplugin inside (the patcher repairs mismatched
    downloads for the same reason). Building from one derived id -- folder,
    .uplugin and container all -- makes a mismatch impossible.

    Capped at 40 characters: the id lands in the install path TWICE (folder
    and container file name), and past that a deep Steam path crosses
    Windows' 260-character limit -- the game then cannot open the container
    and the mod silently never appears in the menu (field report: a long
    "mod - outfit" name; shortening it fixed the mod). Display names are
    untouched, only the id shrinks.
    """
    cleaned = "".join(c for c in name if c.isalnum() or c == "_")
    return cleaned[:40] or "Mod"


def safe_plugin_id(name, used):
    """plugin_id, kept unique across one drop -- two outfits whose names
    differ only in punctuation would otherwise collide."""
    base = plugin_id(name)
    out, n = base, 1
    while out.lower() in used:
        n += 1
        out = f"{base[:40 - len(str(n))]}{n}"
    used.add(out.lower())
    return out
