"""
roundtrip.py -- developer test rig. Not part of the user-facing tools.

Drop a folder of Dresscode mods on this (unpacked folders, archives, or a
mix). Every mod found is converted to loose paks and back inside a
TEMPORARY sandbox -- nothing dropped is read-modified or overwritten, and
the sandbox is deleted as each mod finishes -- then the rebuilt plugin is
compared with the original layer by layer:

    files    every file in the plugin folder, raw bytes (uplugin, icon, ...)
    utoc     header, chunk ids, offsets, directory index, checksum table
    chunks   every chunk's decompressed payload
    pak      mount, path-hash seed, every entry's path and content

Compressed streams are allowed to differ (same content, different Oodle
output -- the checksum table certifies the content). Everything else must
match byte for byte, or the mod is reported as a failure with the reason.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "lib"))

import convert                                                  # noqa: E402
import drops                                                    # noqa: E402
import iostore                                                  # noqa: E402
import pakfile                                                  # noqa: E402
from iostore import Toc                                         # noqa: E402

CONTAINER_EXTS = (".utoc", ".ucas", ".pak")


def compare(orig_root, back_root, plugin):
    """Every way the rebuilt plugin differs from the original that MATTERS.
    Returns a list of problem strings, empty when the round trip is
    lossless."""
    problems = []

    def walk(root):
        out = {}
        for r, _d, files in os.walk(root):
            for f in files:
                p = os.path.join(r, f)
                out[os.path.relpath(p, root).replace("\\", "/")] = p
        return out

    a, b = walk(orig_root), walk(back_root)
    trio = {f"Content/Paks/WindowsNoEditor/{plugin}End-WindowsNoEditor{e}"
            for e in CONTAINER_EXTS}
    for rel in sorted(set(a) - set(b)):
        problems.append(f"missing from rebuild: {rel}")
    for rel in sorted(set(b) - set(a)):
        problems.append(f"extra in rebuild: {rel}")
    for rel in sorted(set(a) & set(b) - trio):
        with open(a[rel], "rb") as fa, open(b[rel], "rb") as fb:
            if fa.read() != fb.read():
                problems.append(f"file differs: {rel}")

    utoc = f"Content/Paks/WindowsNoEditor/{plugin}End-WindowsNoEditor.utoc"
    if utoc not in a or utoc not in b:
        return problems + ["container missing"]
    ta, tb = Toc(a[utoc]), Toc(b[utoc])
    try:
        da, db = ta.d, tb.d
        for label, ok in [
                ("utoc header", da[:144] == db[:144]),
                ("chunk ids", [bytes(x) for x in ta.chunk_ids]
                 == [bytes(x) for x in tb.chunk_ids]),
                ("chunk offsets/lengths", ta.offlen == tb.offlen),
                ("directory index", ta.dir_raw == tb.dir_raw),
                ("checksum table",
                 da[ta.meta_off:ta.meta_off + ta.n * 33]
                 == db[tb.meta_off:tb.meta_off + tb.n * 33]),
                ("compression method names", ta.methods == tb.methods)]:
            if not ok:
                problems.append(f"{label} differs")
        if ta.n == tb.n:
            bad = sum(1 for i in range(ta.n) if ta.read(i) != tb.read(i))
            if bad:
                problems.append(f"{bad} of {ta.n} chunk payloads differ")
    finally:
        ta.close()
        tb.close()

    pak = utoc[:-5] + ".pak"
    try:
        with open(a[pak], "rb") as f:
            ma, sa, fa = pakfile.read_entries(f.read(),
                                              iostore.oodle_decompress)
        with open(b[pak], "rb") as f:
            mb, sb, fb = pakfile.read_entries(f.read(),
                                              iostore.oodle_decompress)
        if (ma, sa) != (mb, sb):
            problems.append("pak mount/seed differ")
        if fa != fb:
            problems.append("pak entries differ")
    except Exception as ex:
        problems.append(f"pak unreadable: {ex}")
    return problems


def test_mod(utoc, uplugin, sandbox):
    """One mod through the full pipeline. Returns (problems, log_text)."""
    plugin = os.path.splitext(os.path.basename(uplugin))[0]
    orig_root = os.path.abspath(os.path.dirname(uplugin))
    log = io.StringIO()
    toc = Toc(utoc)
    try:
        with contextlib.redirect_stdout(log):
            runners = convert.prepare_to_loose(toc, uplugin,
                                               out_base=sandbox)
            code = max(run() for run in runners)
    finally:
        toc.close()
    if code:
        return ["forward conversion failed"], log.getvalue()

    loose = os.path.join(sandbox,
                         os.path.basename(orig_root) + " (loose pak)")
    with contextlib.redirect_stdout(log):
        handled = convert.loose_to_dresscode(loose, convert.find_mods(loose),
                                             assume_yes=True)
    if handled != 0:
        return ["return conversion failed"], log.getvalue()
    if "restoring the original exactly" not in log.getvalue():
        return ["restore record was not used (generic build ran)"], \
            log.getvalue()

    back_root = os.path.join(sandbox, f"{plugin} (Dresscode)", plugin)
    if not os.path.isdir(back_root):
        return ["rebuilt plugin folder not found"], log.getvalue()
    return compare(orig_root, back_root, plugin), log.getvalue()


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print(__doc__.strip())
        return 2

    temps = []
    found = []
    try:
        for raw in args:
            source = os.path.abspath(raw.rstrip("\\/"))
            if not os.path.exists(source):
                print(f"  Not found: {source}")
                continue
            for utoc, uplugin, _out in convert.gather(source, temps):
                found.append((utoc, uplugin))

        mods = [(u, p) for u, p in found if p]
        skipped = len(found) - len(mods)
        print()
        print(f"  ROUND TRIP TEST -- {len(mods)} Dresscode mod"
              f"{'s' if len(mods) != 1 else ''}"
              + (f", {skipped} loose pak(s) skipped" if skipped else ""))
        print()

        passed = 0
        for utoc, uplugin in mods:
            plugin = os.path.splitext(os.path.basename(uplugin))[0]
            sandbox = tempfile.mkdtemp(prefix="roundtrip-")
            start = time.time()
            try:
                problems, logtext = test_mod(utoc, uplugin, sandbox)
            except Exception as ex:
                problems, logtext = [f"crashed: {ex}"], ""
            finally:
                shutil.rmtree(sandbox, ignore_errors=True)
            took = time.time() - start
            if not problems:
                passed += 1
                print(f"  PASS  {plugin:<28} lossless   {took:5.1f}s")
            else:
                print(f"  FAIL  {plugin:<28} {problems[0]}   {took:5.1f}s")
                for p in problems[1:6]:
                    print(f"        {p}")
                tail = [l for l in logtext.splitlines() if l.strip()][-4:]
                for l in tail:
                    print(f"        | {l}")
        print()
        print(f"  {passed} of {len(mods)} lossless")
        return 0 if passed == len(mods) and mods else 1
    finally:
        for tmp in temps:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _code = 1
    try:
        _code = main(sys.argv[1:])
    except RuntimeError as ex:
        print(f"  {ex}")
    drops.pause_before_exit(sys.argv[1:], False)
    sys.exit(_code)
