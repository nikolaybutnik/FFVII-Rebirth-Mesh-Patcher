"""
oodle_setup.py -- ask the user for an Oodle DLL, once, and remember it.

Mod archives are Oodle-compressed. The library is proprietary and cannot be
bundled, and FF7 Rebirth links it statically so there is no copy in the game
folder to borrow. detect.find_oodle() looks through installed games first; this
is what happens when that comes up empty.

Rather than print instructions and quit, prompt for the file and copy it next to
the patcher so it is a one-time step. Dragging a file onto a console window
pastes its full path on Windows, which most people already know, so this needs
no path typing and no config editing.

This does not help someone who has no copy at all -- hence the guidance about
where to find one.
"""

import glob
import os
import shutil

OODLE_GLOB = "oo2core_*_win64.dll"

GUIDANCE = """
  Mod archives are compressed with Oodle. It is proprietary, so it cannot be
  included here -- but it ships loose with a number of games. You need
  oo2core_6 or newer (oo2core_5 and older can't decode this game).

  If you own any of these, you already have a working one:

      ELDEN RING                           Game\\oo2core_6_win64.dll
      DOOM Eternal                         oo2core_8_win64.dll
      DEATH STRANDING DIRECTOR'S CUT       oo2core_7_win64.dll
      Indiana Jones and the Great Circle   oo2core_9_win64.dll

  Search your game folders for:  oo2core

  If you have none of them, Unreal Engine ships one and is free from the Epic
  Games Launcher. Install it and this tool finds the DLL on its own -- a large
  download for one file, but it always works.
"""


def looks_like_oodle(path):
    """True for a filename of the form oo2core_*.dll."""
    name = os.path.basename(path).lower()
    return name.startswith("oo2core") and name.endswith(".dll")


def clean_path(raw):
    """
    Turn whatever the user typed or dragged into a usable path.

    Dragging onto a console pastes the path, quoted if it contains spaces.
    PowerShell may also prefix it with & and quote it.
    """
    s = raw.strip()
    if s.startswith("&"):
        s = s[1:].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s.strip()


def prompt_for_oodle(dest_dir, input_fn=input, print_fn=print):
    """
    Ask for the DLL and copy it to dest_dir. Returns the new path, or None.

    Accepts either the DLL itself or a folder containing one, so dragging in a
    game folder also works.
    """
    print_fn(GUIDANCE)
    print_fn("  Drag the file onto this window and press Enter, or paste its path.")
    print_fn("  Press Enter on its own to give up.")
    print_fn("")

    for _ in range(3):
        try:
            raw = input_fn("  > ")
        except (EOFError, KeyboardInterrupt):
            print_fn("")
            return None

        path = clean_path(raw)
        if not path:
            return None

        if os.path.isdir(path):
            # Accept a folder too: a game folder, or a whole Unreal Engine
            # install. Reuse detection so the unversioned oo2core.dll (UE 5.6+)
            # and the deep engine layout are handled the same way here.
            import detect
            found = (detect._oodle_in_dir(path)
                     or detect._oodle_in_engine(path))
            if not found:
                deep = sorted(glob.glob(os.path.join(path, "*", OODLE_GLOB)))
                found = deep[-1] if deep else None
            if not found:
                print_fn("  No Oodle DLL (oo2core) in that folder. Try again.\n")
                continue
            path = found

        if not os.path.isfile(path):
            print_fn("  That file does not exist. Try again.\n")
            continue
        if not looks_like_oodle(path):
            print_fn("  That is not an Oodle DLL (oo2core...). Try again.\n")
            continue
        import detect
        if not detect._oodle_version_ok(path):
            print_fn(f"  {os.path.basename(path)} is too old for this game. "
                     "Use oo2core_6 or newer.\n")
            continue

        dest = os.path.join(dest_dir, os.path.basename(path))
        try:
            if os.path.abspath(path) != os.path.abspath(dest):
                shutil.copy2(path, dest)
        except OSError as ex:
            print_fn(f"  Could not copy it here ({ex}).")
            print_fn(f"  Copy it to {dest_dir} yourself and run again.\n")
            return None

        print_fn(f"\n  Saved as {os.path.basename(dest)} -- "
                 f"you will not be asked again.\n")
        return dest

    print_fn("  Giving up for now.\n")
    return None
