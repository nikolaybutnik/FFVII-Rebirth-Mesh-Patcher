"""
detect.py -- find the game and the Oodle library without asking the user.

Two things this tool needs, and neither should require editing a config file:

  THE GAME FOLDER. Found either by noticing we are sitting inside it, or by
  reading Steam's own library list.

  AN OODLE DECOMPRESSION LIBRARY. Mod archives are Oodle-compressed. FF7
  Rebirth links Oodle statically, so there is no copy in the game folder -- but
  plenty of other games ship one as a loose DLL, and using a copy already on the
  user's disk is both legal and invisible. We are not allowed to redistribute
  it ourselves.

Everything here fails quietly and returns None; the caller falls back to
whatever is set in config.py.
"""

import glob
import os
import re

GAME_FOLDER_NAME = "FINAL FANTASY VII REBIRTH"
OODLE_GLOB = "oo2core_*_win64.dll"


# ---------------------------------------------------------------------------
# Steam
# ---------------------------------------------------------------------------

def steam_root():
    """Steam's install folder, from the registry, falling back to usual spots."""
    try:
        import winreg
        keys = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        )
        for hive, key in keys:
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            v = winreg.QueryValueEx(k, name)[0]
                            if v and os.path.isdir(v):
                                return v
                        except FileNotFoundError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass

    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(guess):
            return guess
    return None


def steam_libraries():
    """Every Steam library folder, including ones on other drives."""
    root = steam_root()
    if not root:
        return []

    libs = [root]
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            # entries look like:   "path"    "D:\\SteamLibrary"
            for m in re.finditer(r'"path"\s*"([^"]+)"', text):
                p = m.group(1).replace("\\\\", "\\")
                if os.path.isdir(p):
                    libs.append(p)
        except OSError:
            pass

    out, seen = [], set()
    for p in libs:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# The game
# ---------------------------------------------------------------------------

def _looks_like_game(path):
    return os.path.isdir(os.path.join(path, "End", "Content", "Paks"))


def find_game(start=None):
    """
    Locate the FF7 Rebirth install.

    First check whether we are sitting inside it -- if this tool was dropped in
    End\\Mods\\ or anywhere under the game folder, walking up finds it and no
    configuration is needed at all. Otherwise ask Steam.
    """
    here = os.path.abspath(start or os.path.dirname(os.path.abspath(__file__)))
    p = here
    for _ in range(8):
        if _looks_like_game(p):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent

    for lib in steam_libraries():
        cand = os.path.join(lib, "steamapps", "common", GAME_FOLDER_NAME)
        if _looks_like_game(cand):
            return cand
    return None


# ---------------------------------------------------------------------------
# Oodle
# ---------------------------------------------------------------------------

def other_game_roots():
    """
    Non-Steam places that commonly contain an Oodle DLL.

    Epic Games titles ship one about as often as Steam ones, and any installed
    Unreal Engine keeps a copy under Engine/Binaries/ThirdParty/Oodle.
    """
    roots = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        base = os.environ.get(env)
        if not base:
            continue
        for sub in ("Epic Games", "GOG Galaxy/Games", "Xbox Games"):
            p = os.path.join(base, *sub.split("/"))
            if os.path.isdir(p):
                roots.append(p)
    for drive in "CDEFG":
        for sub in ("Epic Games", "Games", "XboxGames"):
            p = f"{drive}:\\{sub}"
            if os.path.isdir(p):
                roots.append(p)
    return roots


def find_oodle(extra_dirs=()):
    """
    Find any oo2core_*_win64.dll already on this machine.

    Looks beside this tool first (so a user can simply drop one in), then
    through installed games. Games keep it either in their root or a level or
    two down, so the scan is depth-limited rather than exhaustive -- a full disk
    walk would be far too slow.

    HOW LIKELY THIS IS TO SUCCEED: not guaranteed. Only a minority of games ship
    the DLL loose -- roughly one in twenty on a typical library. They tend to be
    large titles, so many people will have one, but a user may well have none.
    The caller must degrade gracefully and tell them what to do.
    """

    def scan(folder, depth):
        if depth < 0 or not os.path.isdir(folder):
            return None
        hits = sorted(glob.glob(os.path.join(folder, OODLE_GLOB)))
        if hits:
            return hits[-1]          # highest version number sorts last
        if depth == 0:
            return None
        try:
            entries = os.listdir(folder)
        except OSError:
            return None
        for name in entries:
            sub = os.path.join(folder, name)
            if os.path.isdir(sub):
                found = scan(sub, depth - 1)
                if found:
                    return found
        return None

    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.dirname(here)) + tuple(extra_dirs):
        hits = sorted(glob.glob(os.path.join(d, OODLE_GLOB)))
        if hits:
            return hits[-1]

    roots = [os.path.join(l, "steamapps", "common") for l in steam_libraries()]
    roots += other_game_roots()

    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            games = sorted(os.listdir(root))
        except OSError:
            continue
        for g in games:
            found = scan(os.path.join(root, g), 2)
            if found:
                return found

    # Unreal Engine installs keep one in a fixed spot.
    for root in roots:
        for name in ("UE_5.4", "UE_5.3", "UE_5.2", "UE_4.27", "UE_4.26"):
            p = os.path.join(root, name, "Engine", "Binaries", "ThirdParty", "Oodle")
            hit = scan(p, 3)
            if hit:
                return hit
    return None


# Games known to ship the DLL loose, for the "could not find it" message.
# Any game with an oo2core_*_win64.dll works; these are just common ones.
KNOWN_OODLE_GAMES = [
    "ELDEN RING",
    "Grand Theft Auto V Enhanced",
    "DEATH STRANDING DIRECTOR'S CUT",
    "Indiana Jones and the Great Circle",
]


if __name__ == "__main__":
    print("Steam root      :", steam_root())
    print("Steam libraries :")
    for l in steam_libraries():
        print("   ", l)
    print("Game folder     :", find_game())
    print("Oodle DLL       :", find_oodle())
