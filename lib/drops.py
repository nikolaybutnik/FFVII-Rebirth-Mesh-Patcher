"""
drops.py -- drag-and-drop plumbing shared by patch.py and convert.py.

Everything a tool needs to behave well when files are dropped onto it:
knowing whether it owns its console window (so output is not lost when the
window closes), and unpacking the archives a drop may contain -- including
archives nested inside archives, which is how mods are commonly shared.
"""

import os
import zipfile


# ---------------------------------------------------------------------------
# Console ownership and the end-of-run pause
# ---------------------------------------------------------------------------

# Names (lowercase) of interactive shells / terminals. If one of these is
# sharing our console, we were launched from it rather than owning the window.
SHELLS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe",
          "windowsterminal.exe", "openconsole.exe", "bash.exe", "sh.exe",
          "zsh.exe", "fish.exe", "conemu64.exe", "conemuc64.exe",
          "mintty.exe", "alacritty.exe", "wezterm-gui.exe"}


def _console_proc_names():
    """Lowercase exe names of every process attached to this console, or None if
    there is no console (output redirected/piped) or it cannot be queried."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.GetConsoleProcessList.restype = wintypes.DWORD
    count = k32.GetConsoleProcessList((wintypes.DWORD * 1)(), 1)
    if not count:
        return None
    buf = (wintypes.DWORD * (count + 4))()
    count = k32.GetConsoleProcessList(buf, len(buf))
    if not count:
        return None
    pids = set(buf[:count])

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    # restype MUST be HANDLE -- the default c_int truncates the handle on 64-bit
    # and the snapshot walk silently finds nothing.
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)     # TH32CS_SNAPPROCESS
    if snap == ctypes.c_void_p(-1).value:
        return None
    names = []
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.th32ProcessID in pids:
                names.append(e.szExeFile.decode("mbcs", "replace").lower())
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return names


def owns_console():
    """
    True when this process owns the console window -- double-clicked or a
    folder dropped on it, so the window vanishes on exit and the user needs a
    pause to read the output. Decided by WHAT is attached, not how many:
    counting fails because the py.exe launcher stays attached, making a
    double-clicked script two processes with no shell among them.
    """
    if os.name != "nt":
        return False
    try:
        names = _console_proc_names()
    except Exception:
        return False
    if not names:
        return False
    return not any(n in SHELLS for n in names)


def pause_before_exit(argv, interacted=False):
    """Hold the window open when we own it, so double-clickers can read the
    output. Runs on EVERY exit -- listing, errors, "nothing selected" -- not
    just after doing work. `interacted` skips it when a menu already handled
    the final keypress, so the user is not asked for a second Enter."""
    if interacted or "--no-pause" in argv:
        return
    if "--pause" in argv or owns_console():
        try:
            input("Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------

# Archives a drop may contain; unpacked by extract_archive.
ARCHIVE_EXTS = (".zip", ".7z", ".rar")


def is_archive(source):
    return source.lower().endswith(ARCHIVE_EXTS)


def archives_in(source):
    """Every archive inside a dropped FOLDER -- mods are often shared as a
    folder of per-mod archives, whose contents only appear once unpacked."""
    if not os.path.isdir(source):
        return []
    return sorted(os.path.join(dp, f)
                  for dp, _dn, fn in os.walk(source) for f in fn
                  if is_archive(f))


def contains_archive(source):
    return bool(archives_in(source))


def _tar_exe():
    """Windows 10 and 11 bundle tar (libarchive), which reads zip, 7z and rar
    with nothing installed."""
    tar = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "tar.exe")
    if os.path.exists(tar):
        return tar
    import shutil as _sh
    return _sh.which("tar")


def archive_listing(src):
    """Every path inside `src`, read from its index without unpacking -- fast
    even on a multi-gigabyte archive. Empty if the index cannot be read; the
    extraction attempt is what reports why."""
    if src.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(src) as z:
                return z.namelist()
        except Exception:
            pass                        # fall through to tar, which may cope
    tar = _tar_exe()
    if not tar:
        return []
    import subprocess
    try:
        r = subprocess.run([tar, "-tf", src], capture_output=True, text=True)
    except OSError:
        return []
    return r.stdout.splitlines() if r.returncode == 0 else []


def archive_summary(src):
    """What an archive holds, as (mod names, archives nested inside).

    Lets a drop show its contents before anything is unpacked. A loader mod is
    named by its .uplugin -- the name Dresscode looks for, and the one it will
    be renamed to, which is often not the name the download arrived under.
    """
    mods, inner = set(), set()
    for entry in archive_listing(src):
        path = entry.replace("\\", "/").rstrip("/")
        stem, ext = os.path.splitext(path.rsplit("/", 1)[-1])
        ext = ext.lower()
        if ext == ".uplugin":
            mods.add(stem)
        elif ext == ".utoc":
            # A loader mod's container sits under Content/Paks/WindowsNoEditor
            # and is named for the container, not the mod -- its .uplugin above
            # is the real name, so only paks are named from the .utoc.
            if "content/paks/windowsnoeditor" not in path.lower():
                mods.add(stem)
        elif is_archive(path):
            inner.add(path.rsplit("/", 1)[-1])
    return sorted(mods), sorted(inner)


def show_archive(src, indent):
    """Print what `src` holds, for the listing shown before a drop menu."""
    mods, inner = archive_summary(src)
    for m in mods:
        print(f"{indent}{m}")
    for i in inner:
        print(f"{indent}{i}   (unpacks to more)")
    if not mods and not inner:
        print(f"{indent}(cannot see inside this one until it is unpacked)")


def _archive_tools(src, dst):
    """Command lines that can unpack `src` into `dst`, best first. tar handles
    all three formats with nothing installed; a 7-Zip install is the fallback
    for anything older than Windows 10."""
    import shutil as _sh
    tar = _tar_exe()
    if tar:
        yield tar, [tar, "-xf", src, "-C", dst]
    seven = (_sh.which("7z") or _sh.which("7za")
             or next((p for p in (r"C:\Program Files\7-Zip\7z.exe",
                                   r"C:\Program Files (x86)\7-Zip\7z.exe")
                      if os.path.exists(p)), None))
    if seven:
        yield seven, [seven, "x", src, f"-o{dst}", "-y"]


def extract_archive(src, dst):
    """Extract `src` into `dst`. A .zip goes through the standard library first,
    then falls back to the same tools as .7z/.rar -- tar reads zip too, and
    copes with ones zipfile rejects. Raises with a readable reason, and a way
    forward, if nothing can unpack it."""
    os.makedirs(dst, exist_ok=True)
    reasons = []
    if src.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(src) as z:
                z.extractall(dst)
            return
        except Exception as ex:
            reasons.append(str(ex))
    import subprocess
    for tool, argv in _archive_tools(src, dst):
        try:
            r = subprocess.run(argv, capture_output=True, text=True)
        except OSError as ex:
            reasons.append(str(ex))
            continue
        if r.returncode == 0 and any(os.scandir(dst)):
            return
        reasons.append(r.stderr.strip() or r.stdout.strip() or "nothing extracted")
    raise RuntimeError(
        "could not unpack it -- " + ("; ".join(reasons) if reasons else
        f"no tool for {os.path.splitext(src)[1]} files") +
        ". You can unpack it yourself and drop the folder instead.")


def expand_archives(root, max_depth=4):
    """Unpack archives inside `root`, repeatedly -- mods are often shared as an
    archive (or folder) of per-mod archives, which a single pass would leave
    unopened. Each archive becomes a folder and is removed. Runs only on our
    own copy, never the original."""
    failed = set()
    for _ in range(max_depth):
        found = [p for p in (os.path.join(dp, f)
                             for dp, _dn, fn in os.walk(root) for f in fn)
                 if is_archive(p) and p not in failed]
        if not found:
            return
        for arc in found:
            print(f"  Unpacking {os.path.basename(arc)} ...")
            try:
                extract_archive(arc, os.path.splitext(arc)[0])
            except Exception as ex:
                print(f"  Could not unpack {os.path.basename(arc)}: {ex}")
                failed.add(arc)
                continue
            os.remove(arc)
