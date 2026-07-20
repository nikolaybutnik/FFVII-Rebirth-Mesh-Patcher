"""
Paths. In most cases you should not need to touch this file.

The tool finds what it needs by itself:

  THE GAME      by noticing it is running from inside the game folder, or by
                reading Steam's library list.
  THE OODLE DLL by looking beside this tool, then through your installed Steam
                games. Mod archives are Oodle-compressed, and FF7 Rebirth links
                Oodle statically so there is no copy in the game folder. Many
                other games ship one as a loose file, and using a copy already
                on your disk is both legal and invisible.

If detection fails, either drop an oo2core_*_win64.dll next to patch.py, or
fill in the two values below by hand.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import detect


# ---------------------------------------------------------------------------
# Manual overrides. Leave as None to auto-detect.
# ---------------------------------------------------------------------------
OODLE_DLL = None
GAME_DIR = None


# ---------------------------------------------------------------------------
# Detection fills in whatever was left as None.
# ---------------------------------------------------------------------------
_detected_game = False
_detected_oodle = False

if not GAME_DIR:
    _found = detect.find_game(_HERE)
    if _found:
        GAME_DIR, _detected_game = _found, True

if not OODLE_DLL:
    _found = detect.find_oodle()
    if _found:
        OODLE_DLL, _detected_oodle = _found, True

GAME_DIR = GAME_DIR or ""
OODLE_DLL = OODLE_DLL or ""


def _derive():
    global GAME_PAKS, GLOBAL_UTOC, MODS_DIR
    GAME_PAKS = os.path.join(GAME_DIR, "End", "Content", "Paks")
    GLOBAL_UTOC = os.path.join(GAME_PAKS, "global.utoc")
    MODS_DIR = os.path.join(GAME_DIR, "End", "Mods")


_derive()


# ---------------------------------------------------------------------------
# Local override, so a working copy can hold real paths without committing them.
# Create config_local.py next to this file; anything it defines wins.
# ---------------------------------------------------------------------------
try:
    import config_local as _local

    for _k in dir(_local):
        if not _k.startswith("_"):
            globals()[_k] = getattr(_local, _k)
    if getattr(_local, "GAME_DIR", None):
        _detected_game = False
        _derive()
    if getattr(_local, "OODLE_DLL", None):
        _detected_oodle = False
except ImportError:
    pass


def check():
    """Return a list of problems; empty means everything is usable."""
    problems = []

    if not GAME_DIR:
        problems.append(
            "Could not find FINAL FANTASY VII REBIRTH.\n"
            "    Set GAME_DIR in config.py, or put this tool inside the game folder.")
    elif not os.path.isdir(GAME_PAKS):
        problems.append(f"Game folder looks wrong (no End\\Content\\Paks): {GAME_DIR}")

    if not OODLE_DLL:
        games = "\n".join(f"        - {g}" for g in detect.KNOWN_OODLE_GAMES)
        problems.append(
            "Could not find an Oodle library (oo2core_*_win64.dll).\n"
            "\n"
            "    This tool needs it to read mod archives. It is proprietary, so it\n"
            "    cannot be bundled -- but it ships loose with a number of games and\n"
            "    any version works. If you own one of these, you already have it:\n"
            f"{games}\n"
            "\n"
            "    Find the file (search your game folders for 'oo2core'), copy it\n"
            "    next to patch.py, and run again. Nothing else to configure.")
    elif not os.path.exists(OODLE_DLL):
        problems.append(f"Oodle DLL not found: {OODLE_DLL}")

    return problems


def describe():
    """Lines showing where things were found, for the tool to print."""
    return [
        f"  Game   ({'detected' if _detected_game else 'configured'}):  "
        f"{GAME_DIR or '<not found>'}",
        f"  Oodle  ({'detected' if _detected_oodle else 'configured'}):  "
        f"{OODLE_DLL or '<not found>'}",
    ]


if __name__ == "__main__":
    for line in describe():
        print(line)
    issues = check()
    print()
    if issues:
        print("Problems:")
        for i in issues:
            print("  -", i)
    else:
        print("All good.")
