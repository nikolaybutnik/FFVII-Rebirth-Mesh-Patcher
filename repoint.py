"""
repoint.py -- point a pak mod at a different costume or weapon.

    python repoint.py "D:\\mods\\Some Pak Mod"

Or drag the mod onto repoint.py -- a folder, several at once, or a
.zip/.7z/.rar. It says which stock costume or weapon each pak replaces,
lets you pick different ones (as many as you like), and writes a repointed
copy beside the original. Originals are never touched.

Paks only. A Dresscode mod already lets you pick the outfit in its menu, so
there is nothing here for it -- run convert.py on one first if you want it
as paks.
"""
# HOW IT WORKS (not printed -- the docstring above doubles as the usage text)
# ---------------------------------------------------------------------------
# A ~mods pak replaces a particular costume because of the paths inside it,
# and nothing else. lib/slots.py renames those paths onto another stock
# folder and lib/loosepak.py writes the result. Everything hard about that
# -- the eight places a package path is recorded -- is rename.py's, which
# the conversion flow has used since 1.0.
#
# Deliberately NOT here: anything Dresscode, mesh patching, or in-place
# editing. This only ever creates.

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import config                                                   # noqa: E402
import drops                                                    # noqa: E402
import iostore                                                  # noqa: E402
import rename                                                   # noqa: E402
import slots                                                    # noqa: E402

SUFFIX = " (Repointed)"

# Set once a prompt has handled the final keypress, so the end-of-run pause
# does not demand a second Enter.
_INTERACTED = False


# ---------------------------------------------------------------------------
# Finding the paks in a drop
# ---------------------------------------------------------------------------

def find_paks(root):
    """Every .utoc under `root`, skipping anything we wrote there before --
    dropping a folder twice must not repoint the last result as well."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in sorted(dirs) if SUFFIX.strip() not in d]
        for f in sorted(files):
            if f.lower().endswith(".utoc"):
                out.append(os.path.join(dirpath, f))
    return out


def is_plugin(utoc):
    """A .uplugin above a container makes it a Dresscode mod, not a pak."""
    folder = os.path.dirname(utoc)
    for _ in range(4):                          # the container sits 3 deep
        try:
            if any(f.lower().endswith(".uplugin") for f in os.listdir(folder)):
                return True
        except OSError:
            return False
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent
    return False


def unpack(source, temps):
    """A dropped archive, unpacked into a temp folder we own."""
    tmp = tempfile.mkdtemp(prefix="repoint_")
    temps.append(tmp)
    dest = os.path.join(tmp, os.path.splitext(os.path.basename(source))[0])
    print(f"  Unpacking {os.path.basename(source)} ...")
    drops.extract_archive(source, dest)
    drops.expand_archives(dest)
    return dest


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------

def parse_choice(answer, count):
    """
    "1,3", "1 3", "2-5" or "all" as a list of 1-based numbers, or None if it
    is not any of those. Blank means "leave this one alone" and comes back
    empty.
    """
    answer = answer.strip().lower()
    if not answer:
        return []
    if answer == "all":
        return list(range(1, count + 1))
    picked = []
    for part in answer.replace(",", " ").split():
        lo, _, hi = part.partition("-")
        try:
            span = range(int(lo), int(hi or lo) + 1)
        except ValueError:
            return None
        for n in span:
            if not 1 <= n <= count:
                return None
            if n not in picked:
                picked.append(n)
    return picked


def ask(kind, src, paks):
    """
    Offer the slots `src` can move to and return the chosen folder names, in
    the order given -- the first is where the shared files land.
    """
    global _INTERACTED
    options = slots.choices_for(kind, src)
    if not options:
        print(f"  Nothing to move {src} to -- the game has no other "
              f"{kind} for that character.")
        return []

    who = slots.character_name(src) or slots.character_of(kind, src)
    print()
    print(f"  {who}'s {slots.label(src)}   "
          f"({len(paks)} pak{'s' if len(paks) != 1 else ''})")
    for i, folder in enumerate(options, 1):
        now = f"   {slots.label(folder):<22}  <- replaced now" \
            if folder == src else f"   {slots.label(folder)}"
        print(f"    {i:2}{now}")
    print()
    while True:
        try:
            answer = input("  Which should it replace?  numbers, \"all\", "
                           "or Enter to skip: ")
        except (EOFError, KeyboardInterrupt):
            print()
            _INTERACTED = True
            return []
        _INTERACTED = True
        picked = parse_choice(answer, len(options))
        if picked is not None:
            return [options[n - 1] for n in picked]
        print(f"    Not a choice. Give numbers from 1 to {len(options)}.")


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def out_root(source):
    """Where a drop's repointed copy goes: beside it, under one new folder."""
    source = source.rstrip("\\/")
    if os.path.isdir(source):
        return source + SUFFIX
    stem = os.path.splitext(os.path.basename(source))[0]
    return os.path.join(os.path.dirname(source), stem + SUFFIX)


def describe(pak):
    """One line saying what a pak replaces now."""
    what = ", ".join(slots.label(f) for f in sorted(pak["folders"]))
    return f"    {pak['stem']:<34} {pak['kind']}  {what}"


def build(pak, choices, written_to):
    """Write one repointed pak. Returns 0, or 1 with the reason printed."""
    picked = {src: choices[(pak["kind"], src)]
              for src in pak["folders"] if choices.get((pak["kind"], src))}
    # Asking for the slot it already replaces is not a change, and writing
    # a byte-for-byte copy under the same name would only be confusing.
    picked = {src: t for src, t in picked.items() if t != [src]}
    if not picked:
        return 0
    where = ", ".join(slots.label(f) for t in picked.values() for f in t)
    print(f"    {pak['stem']}  ->  {where}")
    out_dir = os.path.join(pak["out_root"], pak["rel"])
    cid = pak["stem"] + "_" + "_".join(
        f for targets in picked.values() for f in targets)
    written = slots.repoint(pak["toc"], pak["packages"], pak["kind"], picked,
                            out_dir, pak["stem"], say=print,
                            container_name=cid)
    problems = rename.verify(written)
    if problems:
        print(f"  PROBLEM -- {pak['stem']} is not sound:")
        for p in problems[:6]:
            print(f"    {p}")
        return 1
    mb = os.path.getsize(os.path.splitext(written)[0] + ".ucas") / (1024 * 1024)
    print(f"      written  ({mb:,.2f} MB, verified)")
    written_to.add(pak["out_root"])
    return 0


def gather(source, temps, tocs, plugins):
    """Every pak in one drop, read and surveyed. Prints what it turns away."""
    root = unpack(source, temps) if drops.is_archive(source) else source
    found = find_paks(root) if os.path.isdir(root) else [
        os.path.splitext(root)[0] + ".utoc"]

    out = []
    for utoc in found:
        stem = os.path.splitext(os.path.basename(utoc))[0]
        if not os.path.exists(utoc):
            print(f"    {stem:<34} no .utoc beside it")
            continue
        if is_plugin(utoc):
            plugins.append(stem)
            print(f"    {stem:<34} a Dresscode mod -- its menu already lets "
                  "you pick")
            continue
        try:
            toc = iostore.Toc(utoc)
            tocs.append(toc)
            packages = rename.read_packages(toc)
        except Exception as ex:
            print(f"    {stem:<34} could not read it: {ex}")
            continue
        kind, folders, _others = slots.survey(packages)
        if not kind:
            print(f"    {stem:<34} replaces no costume or weapon -- "
                  "left out")
            continue
        base = os.path.dirname(utoc)
        out.append(dict(
            utoc=utoc, stem=stem, toc=toc, packages=packages, kind=kind,
            folders=folders, out_root=out_root(source),
            rel=os.path.relpath(base, root) if os.path.isdir(root) else "."))
    return out


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print(__doc__.strip())
        return 2

    temps, tocs, paks, plugins, code = [], [], [], [], 0
    try:
        for raw in args:
            source = os.path.abspath(raw.rstrip("\\/"))
            if not os.path.exists(source):
                print(f"  Not found: {source}")
                code = 1
                continue
            print()
            print(f"  {os.path.basename(source)}")
            found = gather(source, temps, tocs, plugins)
            for pak in found:
                print(describe(pak))
            paks += found

        if not paks:
            print()
            print("  No pak that replaces a costume or a weapon.")
            if plugins:
                print("  Run convert.py on it first if you want it as paks.")
            return code or 1

        wanted = sorted({(p["kind"], src) for p in paks for src in p["folders"]})
        choices = {}
        for kind, src in wanted:
            using = [p for p in paks if src in p["folders"]]
            choices[(kind, src)] = ask(kind, src, using)

        if not any(choices.values()):
            print()
            print("  Nothing to do.")
            return code

        print()
        if not slots.have_game():
            print("  (No game installed, so I cannot check which replaced "
                  "game files")
            print("   have no equivalent on the new costume. Everything else "
                  "works.)")
            print()
        written_to = set()
        for pak in paks:
            code = max(code, build(pak, choices, written_to))

        print()
        if not written_to:
            print("  Nothing written -- that is what these replace already.")
            return code
        print("  Done. The repointed copies are in:")
        for r in sorted(written_to):
            print(f"    {r}")
        print("  Put a pak's three files in the game's "
              "End\\Content\\Paks\\~mods folder.")
        return code
    finally:
        for toc in tocs:                # open handles block temp deletion
            toc.close()
        for tmp in temps:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _code = 1
    try:
        # Only the Oodle DLL is needed. The list of costumes rides along in
        # lib/stockslots.py, so a machine with no game still gets the menu.
        problems = config.check(require_game=False)
        if problems:
            for p in problems:
                print(f"  {p}")
        else:
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
