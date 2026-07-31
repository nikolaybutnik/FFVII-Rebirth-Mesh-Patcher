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

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import drops                                                    # noqa: E402
import iostore                                                  # noqa: E402
import moddata                                                  # noqa: E402
import pakfile                                                  # noqa: E402
import rename                                                   # noqa: E402
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


def plan_to_loose(toc, plugin):
    """
    Rename map for Dresscode -> loose pak.

    The outfit's mesh takes over the character's DEFAULT costume package, so it
    is worn without a menu. Everything else moves into a subfolder of that same
    costume folder, named for the mod: /<Mod>/... names would keep their IDs,
    but the loader silently skips imports into an unmounted root, so they must
    live under /Game/ to resolve from ~mods.
    """
    assets = moddata.find_data_assets(toc)
    if "character" not in assets:
        raise RuntimeError("no Dresscode outfit data in this mod")
    outfits = moddata.read_outfits(toc.read(assets["character"]))
    wearable = [o for o in outfits
                if o["skeletal_mesh"] and not o["skeletal_mesh"].startswith("/Game/")]
    if not wearable:
        raise RuntimeError("this mod registers no mesh of its own to convert")

    outfit = wearable[0]
    mesh = outfit["skeletal_mesh"].split(".")[0]
    target = moddata.default_costume_package(outfit["player_type"])
    if not target:
        raise RuntimeError(f"unknown character {outfit['player_type']!r}")

    # The registration assets are Dresscode's, and only Dresscode's. Carried
    # into a loose pak they become data assets whose CLASS lives in a plugin
    # that is not mounted when ~mods is -- and the loader still enumerates them
    # at startup. They describe a mod that, in this format, does not exist.
    registration = set() if KEEP_REGISTRATION else set(assets.values())

    packages = rename.read_packages(toc)
    own = {p["name"].lower() for p in packages.values()}

    # ".../PC0002_00_Tifa_Standard" -- the costume folder the mesh lives in.
    costume_root = target.rsplit("/Model/", 1)[0]

    renames, objects, drop = {}, {}, set()
    for info in packages.values():
        if info["chunk"] in registration:
            drop.add(info["name"])
        name = info["name"]
        if name.lower() == mesh.lower():
            renames[name.lower()] = target
            # The mesh's EndCharacterConditionUserData soft-references a
            # condition (petrify) mesh, and Dresscode authors can leave it
            # pointing into their own uncooked project folder -- Dresscode never
            # follows the reference, but the stock costume pipeline does. Point
            # it at the condition mesh of the costume being replaced.
            for cond in condition_refs(toc, info["chunk"], own):
                if cond.lower() != f"{target}_Condition".lower():
                    renames[cond.lower()] = f"{target}_Condition"
            # The object inside has to take the stock name too. The game imports
            # /Game/.../PC0002_00.PC0002_00, and an import ID hashes the object
            # name with the package's -- so a mesh still called MyOutfit answers
            # to an ID nothing asks for, and the override silently does nothing.
            stock_object = target.rsplit("/", 1)[-1]
            objects[name.lower()] = {e: stock_object for e in info["exports"]
                                     if "/" not in e and e != stock_object
                                     and e == mesh.rsplit("/", 1)[-1]}
        elif name.lower().startswith(f"/{plugin.lower()}/"):
            renames[name.lower()] = f"{costume_root}/{plugin}{name[len(plugin) + 1:]}"
        else:
            renames[name.lower()] = name
    return renames, objects, drop, outfit, target, len(wearable)


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


def prepare_to_loose(toc, uplugin, out_base=None):
    """
    Plan one Dresscode -> loose conversion and print its summary. Returns a
    zero-argument callable that performs it -- planning is separated from
    writing so a multi-mod drop can show everything before one confirmation.

    `out_base` overrides where the output folder goes: beside the mod's own
    folder normally, but a mod unpacked from an archive lives in a temp
    folder, so its output belongs beside the archive instead.
    """
    plugin = os.path.splitext(os.path.basename(uplugin))[0]
    renames, objects, drop, outfit, target, count = plan_to_loose(toc, plugin)

    source_root = os.path.abspath(os.path.dirname(uplugin)).rstrip("\\/")
    out_dir = (os.path.join(out_base, os.path.basename(source_root) + " (loose pak)")
               if out_base else source_root + " (loose pak)")
    base = f"{plugin}_P"

    print()
    print(f"  Mod        : {plugin}   (Dresscode plugin)")
    print(f"  Outfit     : {outfit['name'] or '(unnamed)'}   "
          f"{outfit['player_type'].split('::')[-1].title()}")
    if count > 1:
        print(f"               this mod has {count} outfits; a loose pak holds one,")
        print(f"               so the first is used and the rest are dropped")
    print(f"  Replaces   : {target}")
    print(f"  Writes     : {out_dir}{os.sep}{base}.utoc/.ucas/.pak")

    def run():
        written = rename.rename_container(toc, renames, CONTAINER_MOUNT,
                                          loose_path, out_dir, base,
                                          container_name=base,
                                          object_renames=objects,
                                          drop=drop, fix_arcs=True)
        with open(os.path.join(out_dir, base + ".pak"), "wb") as f:
            f.write(pakfile.build(pakfile.LOOSE_MOUNT))
        print(f"    written {base}.pak")

        problems = rename.verify(written)
        if problems:
            print()
            print("  PROBLEM -- the converted mod is not sound, do not install it:")
            for p in problems[:8]:
                print(f"    {p}")
            if len(problems) > 8:
                print(f"    ... and {len(problems) - 8} more")
            return 1
        print(f"    checked  {os.path.basename(written)} is internally consistent")
        return 0

    return run


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
    try:
        for raw in args:
            source = os.path.abspath(raw.rstrip("\\/"))
            if not os.path.exists(source):
                print(f"  Not found: {source}")
                code = 1
                continue
            found = False
            for utoc, uplugin, out_base in gather(source, temps):
                found = True
                if not uplugin:
                    print()
                    print(f"  Mod        : {os.path.basename(utoc)}   (loose pak)")
                    print("  Converting a loose pak into a Dresscode mod is "
                          "not built yet.")
                    print("  It needs the two registration assets written "
                          "from scratch.")
                    code = max(code, 1)
                    continue
                try:
                    toc = iostore.Toc(utoc)
                    tocs.append(toc)
                    runners.append(prepare_to_loose(toc, uplugin, out_base))
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
            return code or 1

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
