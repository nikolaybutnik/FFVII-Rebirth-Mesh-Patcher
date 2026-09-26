"""
mkrelease.py -- build the two release zips, then prove they work.

    python devtools/mkrelease.py 1.7.3
    python devtools/mkrelease.py 1.7.3 --force        replace existing zips
    python devtools/mkrelease.py 1.7.3 --out=D:	mp   build somewhere else

Writes releases/FFVII-Rebirth-Mesh-Patcher-v<version>.zip (Nexus) and a
-github.zip beside it that also carries the repo files. Each is then unpacked
to a temp folder and checked there, away from the repo: every module under
lib/ is in it, everything compiles, and every script imports. A zip that
fails is deleted, so it cannot be uploaded by mistake.

lib/ is walked, not listed: it has feature subfolders (lib/formats/), and a
flat copy of lib/*.py builds a release that breaks on its first conversion.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = "FFVII-Rebirth-Mesh-Patcher"
SCRIPTS = ["config.py", "patch.py", "unpatch.py", "convert.py", "repoint.py",
           "devtools/parts.py"]
NEXUS = SCRIPTS + ["README.md"]
GITHUB = NEXUS + [".gitignore", "LICENSE", "requirements.txt", "run.bat"]


def lib_files():
    out = []
    for d, subs, fs in os.walk(os.path.join(REPO, "lib")):
        subs[:] = sorted(s for s in subs if s != "__pycache__")
        out += [os.path.relpath(os.path.join(d, f), REPO).replace(os.sep, "/")
                for f in sorted(fs) if f.endswith(".py")]
    return out


def uncommitted(paths):
    """Shipped files that differ from what is committed."""
    p = subprocess.run(["git", "status", "--porcelain", "--"] + paths,
                       capture_output=True, text=True, cwd=REPO)
    return [ln[3:] for ln in p.stdout.splitlines() if ln.strip()]


def build(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            src = os.path.join(REPO, rel)
            info = zipfile.ZipInfo.from_file(src, f"{TOP}/{rel}")
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                z.writestr(info, f.read())


# Run inside the unpacked copy: import every script and every lib module from
# THERE. A module the zip left out fails here instead of in a player's hands.
PROBE = r"""
import importlib, importlib.util, os, sys
root = os.getcwd()
sys.path[:] = [root, os.path.join(root, "lib")] + [
    p for p in sys.path if p and not p.startswith(root)]
bad = []
for d, subs, fs in os.walk(os.path.join(root, "lib")):
    subs[:] = [s for s in subs if s != "__pycache__"]
    for f in fs:
        if f.endswith(".py"):
            rel = os.path.relpath(os.path.join(d, f[:-3]),
                                  os.path.join(root, "lib"))
            try:
                importlib.import_module(rel.replace(os.sep, "."))
            except Exception as ex:
                bad.append(f"lib/{rel}: {type(ex).__name__}: {ex}")
for s in SCRIPTS:
    try:
        spec = importlib.util.spec_from_file_location(
            "probe_" + os.path.basename(s)[:-3], os.path.join(root, s))
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
    except Exception as ex:
        bad.append(f"{s}: {type(ex).__name__}: {ex}")
print("\n".join(bad))
"""


def check(path, files):
    """Problems with the zip at `path`, as a list; empty means it works."""
    problems = []
    with zipfile.ZipFile(path) as z:
        names = {n[len(TOP) + 1:] for n in z.namelist()}
    for rel in files:
        if rel not in names:
            problems.append(f"missing from the zip: {rel}")
    tmp = tempfile.mkdtemp(prefix="release-check-")
    try:
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        root = os.path.join(tmp, TOP)
        pys = [os.path.join(d, f) for d, _s, fs in os.walk(root)
               for f in fs if f.endswith(".py")]
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        p = subprocess.run([sys.executable, "-m", "py_compile"] + pys,
                           capture_output=True, text=True, cwd=root, env=env)
        if p.returncode:
            problems.append("does not compile:\n" + p.stderr.strip())
        p = subprocess.run(
            [sys.executable, "-c", f"SCRIPTS = {SCRIPTS!r}\n" + PROBE],
            capture_output=True, text=True, cwd=root, env=env,
            encoding="utf-8", errors="replace")
        problems += [ln for ln in p.stdout.splitlines() if ln.strip()]
        if p.returncode:
            problems.append(p.stderr.strip()[-800:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    if len(args) != 1:
        print(__doc__.strip())
        return 2
    version = args[0].lstrip("vV")
    lib = lib_files()
    dirty = uncommitted(GITHUB + ["lib"])
    if dirty:
        print("Not committed yet -- commit first, so the release is what "
              "the repo says it is:")
        for d in dirty:
            print(f"  {d}")
        return 1

    out_dir = next((a.split("=", 1)[1] for a in argv
                    if a.startswith("--out=")), os.path.join(REPO, "releases"))
    os.makedirs(out_dir, exist_ok=True)
    plan = [(os.path.join(out_dir, f"{TOP}-v{version}.zip"), NEXUS + lib),
            (os.path.join(out_dir, f"{TOP}-v{version}-github.zip"),
             GITHUB + lib)]
    existing = [p for p, _f in plan if os.path.exists(p)]
    if existing and "--force" not in argv:
        for p in existing:
            print(f"Already there: {os.path.basename(p)}")
        print("Add --force to replace.")
        return 1

    code = 0
    for path, files in plan:
        build(path, files)
        problems = check(path, files)
        name = os.path.basename(path)
        if problems:
            os.remove(path)
            print(f"FAILED  {name} -- deleted:")
            for pr in problems:
                print(f"  {pr}")
            code = 1
        else:
            print(f"ok      {name}   ({len(files)} files, "
                  f"{os.path.getsize(path) / 1024:,.0f} KB)")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
