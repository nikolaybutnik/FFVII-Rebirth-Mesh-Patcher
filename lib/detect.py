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
    Unreal Engine keeps a copy somewhere under Engine/Binaries (the exact spot
    varies by version -- see _oodle_in_engine).
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


def _oodle_loadable(path):
    """
    True only if `path` loads in THIS Python and exports OodleLZ_Decompress.

    This is the arbiter for an ambiguous candidate. Recent Unreal Engine ships
    an unversioned oo2core.dll with a 32-bit copy sitting right beside the 64-bit
    one; loading the 32-bit file in 64-bit Python raises OSError, so a load-test
    is what keeps it from ever being chosen. Versioned oo2core_*_win64.dll names
    are trusted without this, since the name already pins the architecture.
    """
    try:
        import ctypes
        return hasattr(ctypes.CDLL(path), "OodleLZ_Decompress")
    except OSError:
        return False


def _oodle_in_dir(folder):
    """
    A usable Oodle DLL sitting directly in `folder`, or None.

    Prefers a versioned oo2core_*_win64.dll (games, older Unreal Engine); falls
    back to an unversioned oo2core.dll (Unreal Engine 5.6+) only if it actually
    loads here, which rules out a 32-bit file of the same name.
    """
    hits = sorted(glob.glob(os.path.join(folder, OODLE_GLOB)))
    if hits:
        return hits[-1]                 # highest version number sorts last
    for p in sorted(glob.glob(os.path.join(folder, "oo2core.dll"))):
        if _oodle_loadable(p):
            return p
    return None


def _oodle_in_engine(engine_root):
    """
    A usable Oodle DLL inside one Unreal Engine install, or None.

    The location and filename changed across engine versions, so this searches
    rather than assuming a fixed path. Older engines keep a versioned
    oo2core_*_win64.dll somewhere under Engine\\Binaries; 5.6+ ships an
    unversioned oo2core.dll in the .NET tooling runtimes, alongside a 32-bit
    sibling -- hence the win-x64 filter and the load-test. A full walk of
    Engine\\Binaries is ~15k files, well under a second, so breadth is fine.
    """
    binaries = os.path.join(engine_root, "Engine", "Binaries")
    if not os.path.isdir(binaries):
        return None
    legacy = sorted(glob.glob(os.path.join(binaries, "**", OODLE_GLOB),
                              recursive=True))
    if legacy:
        return legacy[0]
    for p in sorted(glob.glob(os.path.join(binaries, "**", "win-x64", "**", "oo2core.dll"),
                              recursive=True)):
        if _oodle_loadable(p):
            return p
    return None


def find_oodle(extra_dirs=()):
    """
    Find an Oodle core DLL already on this machine: oo2core_*_win64.dll as
    shipped loose with many games, or the unversioned oo2core.dll inside an
    Unreal Engine install.

    Looks beside this tool first (so a user can simply drop one in), then
    through installed games, then through any Unreal Engine install. Game folders
    are scanned depth-limited rather than exhaustively -- a full disk walk would
    be far too slow.

    HOW LIKELY THIS IS TO SUCCEED: not guaranteed. Only a minority of games ship
    the DLL loose. They tend to be large titles, so many people will have one, 
    but a user may well have none. The caller must degrade gracefully and tell 
    them what to do.
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
        hit = _oodle_in_dir(d)
        if hit:
            return hit

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

    # Unreal Engine installs ship an Oodle core too (any version works). The
    # exact path and filename vary by engine version, so enumerate every UE_*
    # under the game roots and search each rather than hardcoding a location.
    for root in roots:
        for engine in sorted(glob.glob(os.path.join(root, "UE_*")), reverse=True):
            hit = _oodle_in_engine(engine)
            if hit:
                return hit
    return None


# Games known to ship the DLL loose, for the "could not find it" message.
# Any game with an oo2core_*_win64.dll works; these are just common ones.
KNOWN_OODLE_GAMES = [
    "ELDEN RING",
    "DOOM Eternal",
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
