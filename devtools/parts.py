"""
parts.py -- take a pak mod apart, leave parts out, put it back together.

A DEV TOOL, not part of the normal patching flow. Drag a mod folder (or its
.utoc) onto it and it lists what the model is made of -- one line per named
part, with its triangle count and the material it uses. Say which parts to leave
out and it writes a fresh copy of the mod with those switched off.

    python devtools\\parts.py "D:\\mods\\SomeMod"              list, then ask
    python devtools\\parts.py "D:\\mods\\SomeMod" --list       just list
    python devtools\\parts.py "D:\\mods\\SomeMod" --omit 3,5-7 no questions asked
    python devtools\\parts.py "D:\\mods\\SomeMod" --omit none  everything back on

HOW A PART IS SWITCHED OFF
--------------------------
Every render section carries the engine's own bDisabled flag, and that is all
this sets -- four bytes per part. The geometry stays exactly where it was, so:

  * nothing is destroyed, and switching a part back on is the same edit again;
  * the .ucas comes out the same size as it went in;
  * the choice is absolute, not cumulative -- what you pick IS the omitted set,
    so re-running with a different answer replaces the previous one.

The original is never written to. The rebuilt mod goes to "parts out" in the
patcher folder (--out picks somewhere else), keeping its .utoc/.ucas/.pak
names, because the game keys off those.
"""

import os
import shutil
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "lib"))

import config                                                    # noqa: E402
import drops                                                     # noqa: E402
import iostore                                                   # noqa: E402
import meshparts                                                 # noqa: E402
import repack                                                    # noqa: E402

# patch.py owns finding mods on disk, the Oodle check and the drop-window
# pause. Borrowing them keeps one copy of each, and keeps this tool's answers
# to "what is a mod" identical to the patcher's.
import patch as patchtool                                        # noqa: E402


# ---------------------------------------------------------------------------
# Finding the mod
# ---------------------------------------------------------------------------

def find_utocs(source):
    """Every .utoc under a dropped path -- or the one that was dropped."""
    src = os.path.abspath(source)
    if os.path.isfile(src):
        stem, ext = os.path.splitext(src)
        if ext.lower() in (".utoc", ".ucas", ".pak"):
            utoc = stem + ".utoc"
            return [utoc] if os.path.exists(utoc) else []
        return []
    if os.path.isdir(src):
        return patchtool._find_pak_utocs(src)
    return []


def mod_label(utoc):
    """What to call this mod on screen: its folder, or the .utoc's own name."""
    folder = os.path.basename(os.path.dirname(os.path.abspath(utoc)))
    if folder.lower() in ("windowsnoeditor", "paks", "content", "~mods", ""):
        return os.path.splitext(os.path.basename(utoc))[0]
    return folder


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def gather(utoc_path):
    """
    (toc, meshes) for one mod, with a flat part numbering across its models.

    Only parts this tool will actually edit are numbered. A model it has to
    refuse -- unreadable, or more than one level of detail -- keeps its parts
    for the listing but stays out of the numbering, so there is no number on
    screen that cannot be acted on.
    """
    toc = iostore.Toc(utoc_path)
    meshes = meshparts.read_container(toc)
    n = 0
    for m in meshes:
        m["editable"] = bool(not m["error"] and m["n_lods"] == 1 and m["parts"])
        if not m["editable"]:
            continue
        for p in m["parts"]:
            n += 1
            p["number"] = n
    return toc, meshes


def editable(meshes):
    """Every part that can be switched off, across all of the mod's models."""
    return [p for m in meshes if m["editable"] for p in m["parts"]]


def shared_slots(mesh):
    """{material: [part numbers]} for materials more than one part uses -- the
    reason hiding one piece by MATERIAL takes several pieces off at once,
    where switching a section off takes exactly the one."""
    by_material = {}
    for p in mesh["parts"]:
        if p["material"]:
            by_material.setdefault(p["material"], []).append(p["number"])
    return {m: nums for m, nums in by_material.items() if len(nums) > 1}


def show(meshes, say=print):
    """Print the parts list. Returns True if anything can be switched off."""
    anything = False
    for m in meshes:
        say("")
        say(f"  {m['path']}   ({m['export']})")
        if m["error"]:
            say(f"      cannot read this model: {m['error']}")
            continue
        if m["n_lods"] != 1:
            say(f"      {m['n_lods']} levels of detail -- this tool only "
                "describes the first, so it will not edit this model (a part "
                "switched off would come back as the camera pulls away)")
            continue
        if not m["parts"]:
            say("      no render sections in this model")
            continue
        anything = True
        if m["old_format"]:
            say("      note: still in the pre-1.005 format -- run patch.py on "
                "this mod too, or the game crashes on it whatever you do here")
        say("")
        say("       #  part                        triangles   material")
        for p in m["parts"]:
            mark = "  [left out]" if p["disabled"] else ""
            material = p["material"].rsplit("/", 1)[-1] if p["material"] else "-"
            say(f"      {p['number']:>2}  {p['slot']:<26} {p['triangles']:>9,}"
                f"   {material}{mark}")
        for material, nums in sorted(shared_slots(m).items()):
            say(f"      ({material.rsplit('/', 1)[-1]} is shared by parts "
                f"{', '.join(str(n) for n in nums)})")
    return anything


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------

def parse_selection(text, meshes):
    """
    Turn "3, 5-7, <name>" into a set of part numbers.

    Returns (numbers, problems). Anything unrecognised lands in `problems` and
    NOTHING is assumed -- a typo that silently omitted the wrong part would be
    discovered in game, which is the expensive place to discover it.
    """
    parts = editable(meshes)
    text = (text or "").strip()
    low = text.lower()
    if low in ("", "none", "no", "keep all", "nothing"):
        return set(), []
    if low == "all":
        return {p["number"] for p in parts}, []

    numbers, problems = set(), []
    for token in text.replace(",", " ").split():
        if "-" in token and all(t.strip().isdigit() for t in token.split("-", 1)):
            lo, hi = (int(t) for t in token.split("-", 1))
            if lo > hi:
                lo, hi = hi, lo
            span = [n for n in range(lo, hi + 1)
                    if any(p["number"] == n for p in parts)]
            if not span:
                problems.append(f"no parts numbered {token}")
            numbers.update(span)
            continue
        if token.isdigit():
            n = int(token)
            if any(p["number"] == n for p in parts):
                numbers.add(n)
            else:
                problems.append(f"there is no part {n}")
            continue
        # A name, which is usually easier than counting rows.
        hits = [p for p in parts if token.lower() in p["slot"].lower()]
        if len(hits) == 1:
            numbers.add(hits[0]["number"])
        elif not hits:
            problems.append(f"no part called {token!r}")
        else:
            which = ", ".join(f"{p['number']} ({p['slot']})" for p in hits)
            problems.append(f"{token!r} matches several parts: {which}")
    return numbers, problems


def ask(meshes):
    """Interactive selection loop. Returns a set of part numbers, or None to
    walk away without writing anything."""
    already = {p["number"] for p in editable(meshes) if p["disabled"]}
    print()
    print("  Which parts should be LEFT OUT?")
    print("     numbers and ranges (3 5-7), or any part of a name")
    print("     'none' puts every part back, 'all' leaves the model empty")
    print("     blank leaves this mod as it is, 'q' quits")
    if already:
        print(f"     currently left out: "
              f"{', '.join(str(n) for n in sorted(already))}")
    while True:
        try:
            answer = input("  > ").strip()
        except EOFError:
            return None
        if answer.lower() in ("q", "quit", "exit"):
            return None
        if not answer:
            return already
        numbers, problems = parse_selection(answer, meshes)
        if problems:
            for p in problems:
                print(f"    {p}")
            print("    nothing was changed -- try again")
            continue
        return numbers


def describe(meshes, chosen):
    """(lines, triangles left out) for a chosen set."""
    lines, tris = [], 0
    for p in editable(meshes):
        if p["number"] in chosen:
            lines.append(f"{p['number']} {p['slot']}")
            tris += p["triangles"]
    return lines, tris


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

OUT_ROOT = os.path.join(_REPO, "parts out")


def output_dir(utoc_path, out_dir=None):
    """
    Where the rebuilt mod goes. Never the folder it came from.

    The default is a fixed folder beside this tool rather than beside the mod,
    and that is deliberate: mods are commonly dropped straight out of
    Paks\\~mods, which the game loads RECURSIVELY -- a result written next to
    its source would be loaded alongside the original, two mods fighting over
    the same character with nothing on screen to say why.
    """
    src_dir = os.path.dirname(os.path.abspath(utoc_path))
    if out_dir:
        dst = os.path.abspath(out_dir)
        if patchtool._same_path(dst, src_dir):
            raise ValueError("that is the mod's own folder -- this tool never "
                             "writes over the original; pick another --out")
        return dst
    stem = os.path.join(OUT_ROOT, f"{mod_label(utoc_path)} (parts omitted)")
    dst, n = stem, 2
    while os.path.exists(dst):
        dst = f"{stem} {n}"
        n += 1
    return dst


def warn_if_installed(dst):
    """A result written into the game install loads BESIDE the original."""
    if not config.GAME_DIR:
        return
    game = os.path.abspath(config.GAME_DIR).lower()
    if os.path.abspath(dst).lower().startswith(game):
        print("    note: that is inside the game install -- move or remove the")
        print("    original mod, or the game loads both copies at once.")


def rebuild(toc, meshes, chosen, utoc_path, dst_dir, say=print):
    """
    Write the mod out with `chosen` parts switched off. Returns True if a
    container was written.
    """
    new_data = {}
    for m in meshes:
        if not m["editable"]:
            continue
        wanted = {p["flag_at"]: p["number"] in chosen for p in m["parts"]}
        data = new_data.get(m["chunk"]) or toc.read(m["chunk"])
        edited, changed = meshparts.apply(data, wanted)
        if changed:
            new_data[m["chunk"]] = edited

    if not new_data:
        say("    already exactly like that -- nothing written")
        return False

    base = os.path.splitext(os.path.basename(utoc_path))[0]
    src_dir = os.path.dirname(os.path.abspath(utoc_path))
    say(f"    rebuilding container ({toc.n} chunks)")
    repack.write(toc, new_data, dst_dir, base, src_dir, copy_pak=True, say=say)
    say(f"    written {base}.utoc/.ucas/.pak  in  {dst_dir}")
    # Where this goes next is the part that is easy to get wrong: as an
    # Optional pak it would become a Dresscode toggle, and a toggle swaps
    # materials, so a model with parts switched off would show no difference
    # at all. Under Variants it becomes an outfit tile, which is a mesh swap.
    say("      To make this a second costume in the same Dresscode mod, put")
    say(f"      these three files in the mod's  Variants\\<name>\\  folder")
    say("      and convert. (NOT Optional -- that is for material swaps.)")
    return True


# ---------------------------------------------------------------------------
# Driving
# ---------------------------------------------------------------------------

def parse_args(argv):
    sources, flags, omit, out_dir = [], set(), None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        # --pause/--no-pause belong to the drop-window handling in patch.py and
        # are read straight from argv there; accepted here so they are not
        # reported as unknown.
        if a in ("--list", "--yes", "-y", "--help", "-h", "--pause",
                 "--no-pause"):
            flags.add(a)
        elif a == "--omit":
            i += 1
            omit = argv[i] if i < len(argv) else ""
        elif a.startswith("--omit="):
            omit = a.split("=", 1)[1]
        elif a in ("--out", "--path"):
            i += 1
            out_dir = argv[i] if i < len(argv) else None
        elif a.startswith("--out="):
            out_dir = a.split("=", 1)[1]
        elif a.startswith("-"):
            print(f"Unknown option {a} -- ignored")
        else:
            sources.append(a)
        i += 1
    return sources, flags, omit, out_dir


def is_extra(utoc, root):
    """A pak under an Optional folder is an EXTRA -- a variant applied on top
    of the outfit, not the outfit itself. Editing one is nearly always the
    wrong move: in Dresscode form an extra usually becomes a material swap
    applied to the OUTFIT's model, so the extra's own model never draws and
    parts switched off in it change nothing on screen."""
    rel = os.path.relpath(os.path.abspath(utoc), root)
    return "optional" in [p.lower() for p in rel.split(os.sep)[:-1]]


def pick_mod(utocs, roots=None):
    """Which mod to work on when a drop contained several."""
    if len(utocs) == 1:
        return utocs[0]
    roots = roots or {}
    print()
    print("  Several paks here:")
    for n, u in enumerate(utocs, 1):
        root = roots.get(u) or os.path.dirname(os.path.dirname(os.path.abspath(u)))
        try:
            where = os.path.relpath(os.path.abspath(u), root)
        except ValueError:
            where = os.path.basename(u)
        tag = "   <- an extra, applied ON TOP of the outfit" \
            if is_extra(u, root) else ""
        print(f"    {n:>2}  {where}{tag}")
    if any(is_extra(u, roots.get(u, "")) for u in utocs):
        print("  The outfit itself is the one NOT under Optional. An extra's")
        print("  model usually never draws once the mod is in Dresscode form.")
    if not interactive():
        print("  Point the tool at one of them, or drop just that folder.")
        return None
    while True:
        try:
            answer = input("  Which one? (number, or blank to quit) > ").strip()
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(utocs):
            return utocs[int(answer) - 1]
        print("    that is not one of the numbers above")


def run_one(utoc_path, flags, omit, out_dir):
    """List, choose and rebuild one mod. Returns an exit code."""
    print()
    print(f"  {mod_label(utoc_path)}")
    try:
        toc, meshes = gather(utoc_path)
    except Exception as ex:
        print(f"    cannot open this mod: {type(ex).__name__}: {ex}")
        return 1

    if not meshes:
        print("    no character model in this mod -- nothing to take apart.")
        print("    (parts.py works on mods that replace a character mesh;")
        print("     texture-only and script-only mods have no parts.)")
        return 1

    # A plugin's packages live under /<Plugin>/, a pak's under /Game/.
    # The rebuilt triple is the same either way, but a plugin is a whole folder
    # with a .uplugin in it, and handing back three files without saying so
    # would leave someone wondering why the mod stopped appearing.
    if any(m["package"] and not m["package"].lower().startswith("/game/")
           for m in meshes):
        print()
        print("    this is a plugin (Dresscode-style), not a pak -- what")
        print("    comes out is the .utoc/.ucas/.pak only, so copy those three")
        print("    over the ones in a copy of the plugin's own")
        print("    Content\\Paks\\WindowsNoEditor folder.")

    anything = show(meshes, say=print)
    if "--list" in flags:
        return 0
    if not anything:
        print()
        print("    nothing here can be edited safely -- left alone.")
        return 1

    if omit is not None:
        chosen, problems = parse_selection(omit, meshes)
        if problems:
            for p in problems:
                print(f"    {p}")
            return 1
    elif interactive():
        chosen = ask(meshes)
        if chosen is None:
            print("    left alone.")
            return 0
    else:
        print()
        print("    --omit was not given and there is nobody to ask "
              "(input is not a terminal).")
        return 1

    every = {p["number"] for p in editable(meshes)}
    if chosen >= every and every:
        print()
        print("    that leaves the model with nothing to draw at all.")
        if "--yes" not in flags and not _confirm("    Write it anyway?"):
            print("    left alone.")
            return 0

    lines, tris = describe(meshes, chosen)
    print()
    if lines:
        print(f"    leaving out: {', '.join(lines)}   ({tris:,} triangles)")
    else:
        print("    leaving out nothing -- every part switched back on")

    try:
        dst = output_dir(utoc_path, out_dir)
    except ValueError as ex:
        print(f"    {ex}")
        return 1
    warn_if_installed(dst)

    try:
        rebuild(toc, meshes, chosen, utoc_path, dst, say=print)
    except PermissionError:
        print(f"    cannot write to {dst} -- it is in use or read-only.")
        return 1
    except OSError as ex:
        print(f"    could not write the mod: {ex}")
        return 1
    return 0


def interactive():
    """Is there somebody to ask? PARTS_ASSUME_TTY lets the tests drive the
    question-and-answer path with piped input, which no real run does."""
    if os.environ.get("PARTS_ASSUME_TTY") == "1":
        return True
    return bool(sys.stdin) and sys.stdin.isatty()


def _confirm(question):
    if not interactive():
        return False
    try:
        return input(f"{question} (y/N) > ").strip().lower().startswith("y")
    except EOFError:
        print()
        return False


def collect(sources, temps, roots):
    """
    Every mod .utoc in everything that was dropped.

    `roots` is filled in with {utoc: the folder it was found under}, so the
    picker can show where each pak sits rather than just its file name --
    which is what tells an outfit apart from an Optional extra when a mod
    ships both under the same file name.

    An archive is unpacked into a temp folder recorded in `temps` -- mods are
    usually shared as .zip, and refusing one would just mean the user unpacking
    it by hand. The rebuilt copy lands in the tool's own output folder either
    way, so nothing depends on the temp folder surviving.
    """
    found = []
    for s in sources:
        if not os.path.exists(s):
            print(f"  There is nothing at {os.path.abspath(s)}")
            continue
        root = s
        if drops.is_archive(s):
            tmp = tempfile.mkdtemp(prefix="parts_")
            temps.append(tmp)
            print(f"  unpacking {os.path.basename(s)}...")
            try:
                drops.extract_archive(s, tmp)
            except Exception as ex:
                print(f"  {os.path.basename(s)}: {ex}")
                continue
            root = tmp
        here = find_utocs(root)
        if not here:
            print(f"  No mod (.utoc) found in {os.path.abspath(s)}")
            continue
        for u in here:
            if u not in found:
                found.append(u)
                roots[u] = os.path.abspath(root if os.path.isdir(root)
                                           else os.path.dirname(root))
    return found


def main(argv):
    sources, flags, omit, out_dir = parse_args(argv)
    if "--help" in flags or "-h" in flags:
        print(__doc__)
        return 0
    if not sources:
        print(__doc__)
        print("  Drop a mod folder onto this file, or name one on the command "
              "line.")
        return 1

    temps, roots = [], {}
    try:
        found = collect(sources, temps, roots)
        if not found:
            return 1
        if len(found) > 1 and (omit is not None or "--list" in flags):
            # Scripted runs act on everything given; only the interactive flow
            # has to ask, because it can hold one conversation at a time.
            code = 0
            for utoc in found:
                code = run_one(utoc, flags, omit, out_dir) or code
            return code
        chosen = pick_mod(found, roots)
        if not chosen:
            return 1
        return run_one(chosen, flags, omit, out_dir)
    finally:
        for t in temps:
            shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    code = 0 if patchtool.startup(require_game=False) else 1
    if code == 0:
        try:
            code = main(sys.argv[1:])
        except KeyboardInterrupt:
            print("\n  stopped.")
            code = 1
    patchtool._pause_before_exit(sys.argv[1:])
    sys.exit(code)
