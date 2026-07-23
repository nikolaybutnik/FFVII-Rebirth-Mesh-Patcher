"""
detect.py -- find the game and the Oodle library without asking the user.

Two things this tool needs, and neither should require editing a config file:

  THE GAME FOLDER. Found either by noticing we are sitting inside it, or by
  reading Steam's own library list.

  AN OODLE DECOMPRESSION LIBRARY. Mod archives are Oodle-compressed. FFVII
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

# FFVII Rebirth's Oodle streams need oo2core_6 or newer. oo2core_5 and older
# return nothing (they can't decode this game), so they must never be selected --
# picking one silently makes every mod look unreadable/unaffected.
OODLE_MIN_VERSION = 6


def _oodle_version_ok(path):
    """
    True if this DLL is new enough for FFVII Rebirth.

    Versioned names (oo2core_<N>_win64.dll) must be N >= OODLE_MIN_VERSION. The
    unversioned oo2core.dll (recent Unreal Engine) has no number and is always
    well above the floor, so it passes.
    """
    m = re.search(r"oo2core_(\d+)_win64\.dll$", os.path.basename(path), re.I)
    return int(m.group(1)) >= OODLE_MIN_VERSION if m else True


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
    Locate the FFVII Rebirth install.

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
    hits = sorted(h for h in glob.glob(os.path.join(folder, OODLE_GLOB))
                  if _oodle_version_ok(h))
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
    legacy = sorted(h for h in glob.glob(os.path.join(binaries, "**", OODLE_GLOB),
                                         recursive=True) if _oodle_version_ok(h))
    if legacy:
        return legacy[0]
    for p in sorted(glob.glob(os.path.join(binaries, "**", "win-x64", "**", "oo2core.dll"),
                              recursive=True)):
        if _oodle_loadable(p):
            return p
    return None


def _oodle_rank(path):
    """
    Sort key for "newest". The oo2core version number; the unversioned oo2core.dll
    from recent Unreal Engine is the current 2.9 generation, so it ranks with
    oo2core_9.
    """
    m = re.search(r"oo2core_(\d+)_win64\.dll$", os.path.basename(path), re.I)
    return int(m.group(1)) if m else 9


def find_oodle(extra_dirs=()):
    """
    The newest usable Oodle DLL on this machine (oo2core_6+), or None.

    All v6+ DLLs decode identically, so newest is just a sensible default. This
    can fail -- only a minority of games ship one -- so callers must degrade
    gracefully. See _oodle_candidates for where it looks.
    """
    candidates = find_all_oodle(extra_dirs)
    return max(candidates, key=_oodle_rank) if candidates else None


def _oodle_in_tree(folder, depth):
    """Highest version-ok oo2core_*_win64.dll within `folder`, depth-limited."""
    if depth < 0 or not os.path.isdir(folder):
        return None
    hits = sorted(h for h in glob.glob(os.path.join(folder, OODLE_GLOB))
                  if _oodle_version_ok(h))
    if hits:
        return hits[-1]
    if depth == 0:
        return None
    try:
        entries = os.listdir(folder)
    except OSError:
        return None
    for name in entries:
        sub = os.path.join(folder, name)
        if os.path.isdir(sub):
            found = _oodle_in_tree(sub, depth - 1)
            if found:
                return found
    return None


def _oodle_candidates(extra_dirs=()):
    """
    Yield every usable Oodle DLL, best first: beside the tool, then one per
    installed game, then one per Unreal Engine install. Game folders are scanned
    depth-limited -- a full disk walk would be far too slow.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.dirname(here)) + tuple(extra_dirs):
        hit = _oodle_in_dir(d)
        if hit:
            yield hit

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
            hit = _oodle_in_tree(os.path.join(root, g), 2)
            if hit:
                yield hit

    for root in roots:
        for engine in sorted(glob.glob(os.path.join(root, "UE_*")), reverse=True):
            hit = _oodle_in_engine(engine)
            if hit:
                yield hit


def find_all_oodle(extra_dirs=()):
    """Every usable Oodle DLL found, deduplicated -- for informational display."""
    out, seen = [], set()
    for path in _oodle_candidates(extra_dirs):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


# Games known to ship a WORKING DLL loose, for the "could not find it" message.
# FFVII Rebirth needs oo2core_6 or newer; oo2core_5 and older can't decode it.
KNOWN_OODLE_GAMES = [
    "ELDEN RING",
    "DOOM Eternal",
    "DEATH STRANDING DIRECTOR'S CUT",
    "Indiana Jones and the Great Circle",
]

# Reported by users to ship one, but unverified.
REPORTED_OODLE_GAMES = [
    "Warhammer 40,000: Darktide",
    "ELDEN RING NIGHTREIGN",
]


if __name__ == "__main__":
    print("Steam root      :", steam_root())
    print("Steam libraries :")
    for l in steam_libraries():
        print("   ", l)
    print("Game folder     :", find_game())
    print("Oodle DLL       :", find_oodle())
