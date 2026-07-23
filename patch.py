"""
patch.py -- FFVII Rebirth mesh patcher.

Fixes mods that were built before game patch V1.005 and no longer load. Works on
costume mods and loose pak mods -- anything containing a skeletal mesh. (Dresscode
itself now has an official V1.005 update, so it is no longer patched here.) Mods
are found in End\\Mods (the FF7RML loader) and in
End\\Content\\Paks\\~mods (loose paks the game loads directly); see find_mods.

    python patch.py --list             show every mod and whether it needs fixing
    python patch.py --all              patch everything that needs it
    python patch.py ModName            patch specific mods by folder or .utoc name
    python patch.py --restore --all    undo everything, from the backups

Originals are copied to ./backups/<ModName>/ before anything is written. The
game and the Oodle library are located automatically; see config.py.

WHAT IT FIXES
-------------
V1.005 changed how skeletal meshes are stored, in three ways:

  1. Render sections no longer carry FDuplicatedVerticesBuffer. Mods still write
     it, so the game's loader desyncs partway through the mesh and reads vertex
     data as though it were structure -- which is why hovering a broken costume
     crashes rather than showing nothing.

  2. The per-vertex tangent frame is now 4 bytes in a new encoding, replacing
     both the 8-byte standard form and the 16-byte high-precision one. Emitting
     the wrong size desyncs the buffer; emitting the wrong VALUES loads fine but
     lights the model wrongly.

  3. Texture coordinates are half floats. Mods that opted into full-precision
     (float32) UVs are read as half by the current shaders, which corrupts every
     texture lookup.

meshfix.py implements all three; this module handles finding mods, rewriting
containers and keeping backups.

WHAT HAS TO STAY CONSISTENT
---------------------------
Removing bytes from the middle of an object is not a local edit. Four things
must be updated together or the package will not load:

  1. the mesh object's recorded size in the package export table;
  2. the recorded offsets of every export stored after it, which all shift;
  3. ExportBundlesSize for that package in the container header;
  4. the container chunk table, directory index and SHA-1 checksums.

Item 4 is handled by a container writer verified to reproduce an untouched
container byte for byte.
"""

import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import config
import dirindex
import iostore
import meshfix
import skm
import writer
import zen

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

# Mods that are part of the loader framework, not content -- never touch these.
SKIP = {"FF7RML", "FF7RModMenu"}

# If this tool was dropped inside End\Mods\ it must not try to patch itself.
_SELF = os.path.basename(os.path.dirname(os.path.abspath(__file__)))


def _find_pak_utocs(root, max_depth=5):
    """Every .utoc under `root`, depth-limited. The game loads paks recursively
    beneath ~mods, and some users nest each mod in its own subfolder."""
    root = os.path.abspath(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath[len(root):].count(os.sep) >= max_depth:
            dirnames[:] = []
        for f in filenames:
            if f.endswith(".utoc"):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def find_mods():
    """Return {mod_name: utoc_path} for every installed mod.

    Mods come from two places, and both are treated the same once found:

      End\\Mods\\<name>\\Content\\Paks\\WindowsNoEditor\\   the FF7RML layout,
                                                            one folder per mod
      End\\Content\\Paks\\~mods\\                           Unreal's loose-pak
                                                            folder, one .utoc
                                                            per mod

    A mod's name is its folder name in the first case and its .utoc filename in
    the second. Names key the backup folders, so a clash would make one mod's
    backup overwrite another's -- add() keeps them unique.
    """
    out = {}

    def add(name, utoc):
        key, n = name, 2
        while key in out and out[key] != utoc:
            key = f"{name} ({n})"
            n += 1
        out[key] = utoc

    # --- Mod-loader mods -------------------------------------------------
    if os.path.isdir(config.MODS_DIR):
        for name in sorted(os.listdir(config.MODS_DIR)):
            if name in SKIP or name == _SELF:
                continue
            d = os.path.join(config.MODS_DIR, name, "Content", "Paks", "WindowsNoEditor")
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if f.endswith(".utoc"):
                    out[name] = os.path.join(d, f)

    # --- Loose pak mods --------------------------------------------------
    paks = getattr(config, "MODS_PAKS_DIR", "")
    if paks and os.path.isdir(paks):
        for utoc in _find_pak_utocs(paks):
            add(os.path.splitext(os.path.basename(utoc))[0], utoc)

    return out


def mod_source(utoc_path):
    """Which folder a mod was found in: 'paks' for ~mods, 'mods' otherwise.
    Derived from the path so it needs no separate bookkeeping."""
    paks = getattr(config, "MODS_PAKS_DIR", "")
    if paks:
        try:
            if os.path.commonpath([os.path.abspath(utoc_path),
                                   os.path.abspath(paks)]) == os.path.abspath(paks):
                return "paks"
        except ValueError:      # different drives -> not under ~mods
            pass
    return "mods"


def scan(utoc_path):
    """
    Report which packages in this container hold skeletal meshes, and whether
    each is in the old (broken) or new layout.
    """
    toc = iostore.Toc(utoc_path)
    found = []
    read_ok = 0     # .uasset chunks that decompressed without error
    parsed = 0      # ...of those, how many parsed as a Zen package
    for i in sorted(toc.paths):
        if not toc.paths[i].endswith(".uasset"):
            continue
        try:
            data = toc.read(i)
        except Exception as ex:
            # A read (decompress) failure is NOT the same as "no mesh here". If
            # the Oodle DLL is too old to decode this game, every chunk fails --
            # swallowing that would make a mod that NEEDS patching look
            # unaffected. Surface it as an error so mod_status reports [??].
            found.append(dict(chunk=i, path=toc.paths[i], export="",
                              size=0, error=f"could not read: {ex}"))
            continue
        read_ok += 1
        try:
            pkg = zen.ZenPackage(data)
        except Exception:
            # Decoded but unparseable -- a wrong Oodle DLL returns garbage of the
            # right length. One odd asset might not parse, so escalate only if
            # NOTHING does (checked after the loop), not here.
            continue
        parsed += 1
        if not any(e["cls"] == skm.SKELETAL_MESH for e in pkg.exports):
            continue

        offset = pkg.export_data_start()
        for e in pkg.exports:
            if e["cls"] == skm.SKELETAL_MESH:
                payload = data[offset:offset + e["size"]]
                try:
                    after, info = skm.parse_head(payload, 0, len(payload),
                                                 skm.NoNames(), verbose=False)
                    lod = skm.parse_lod_header(payload, after)
                    has_dup, _ = meshfix.detect_dup_verts(
                        payload, lod["sections_at"], lod["n_sections"])
                    found.append(dict(chunk=i, path=toc.paths[i], export=e["name"],
                                      size=e["size"], n_lods=lod["n_lods"],
                                      n_sections=lod["n_sections"],
                                      needs_fix=has_dup))
                except Exception as ex:
                    found.append(dict(chunk=i, path=toc.paths[i], export=e["name"],
                                      size=e["size"], error=str(ex)))
                break
            offset += e["size"]

    # Read fine but nothing parsed -> bad decode, not a mesh-free mod.
    if read_ok and not parsed and not found:
        found.append(dict(
            chunk=-1, path="", export="", size=0,
            error="packages did not decode -- the Oodle DLL is likely the "
                  "wrong version for this game"))
    return toc, found


def patch_package(data):
    """
    Convert every skeletal mesh in one package, fixing up the export table.

    Returns (new_bytes, total_removed, reports).
    """
    pkg = zen.ZenPackage(data)
    start = pkg.export_data_start()

    # Slice the export data into per-export payloads.
    payloads = []
    o = start
    for e in pkg.exports:
        payloads.append(bytearray(data[o:o + e["size"]]))
        o += e["size"]

    removed_before = [0] * len(pkg.exports)     # bytes removed prior to export k
    reports = []
    running = 0
    for k, e in enumerate(pkg.exports):
        removed_before[k] = running
        if e["cls"] == skm.SKELETAL_MESH:
            new_payload, report = meshfix.convert_payload(bytes(payloads[k]))
            if report.get("changed"):
                running += report["bytes_removed"]
                payloads[k] = bytearray(new_payload)
            reports.append((e["name"], report))

    if running == 0:
        return data, 0, reports

    # Rebuild: header region unchanged, then the (possibly shrunk) payloads.
    out = bytearray(data[:start])
    for p in payloads:
        out += p

    # Fix up the export table. Each entry is 72 bytes:
    #   +0  CookedSerialOffset (uint64)
    #   +8  CookedSerialSize   (uint64)
    for k, e in enumerate(pkg.exports):
        entry = pkg.exp_off + k * 72
        # Offsets shift down by however much was removed from earlier exports.
        struct.pack_into("<Q", out, entry, e["off"] - removed_before[k])
        struct.pack_into("<Q", out, entry + 8, len(payloads[k]))

    return bytes(out), running, reports


def rebuild_header(header_bytes, size_deltas):
    """
    Update ExportBundlesSize in the container header for packages that shrank.

    size_deltas maps package_id -> bytes removed. The header's layout is
    documented alongside the writer; here we only rewrite one field per
    entry and leave everything else byte-identical.
    """
    out = bytearray(header_bytes)
    count = struct.unpack_from("<I", out, 32)[0]
    ids = struct.unpack_from(f"<{count}Q", out, 36)
    store_base = 36 + count * 8 + 4
    for i in range(count):
        delta = size_deltas.get(ids[i])
        if not delta:
            continue
        o = store_base + i * 32
        old = struct.unpack_from("<Q", out, o)[0]
        struct.pack_into("<Q", out, o, old - delta)
    return bytes(out)


def _pack_blocks(payload, block_size, comp_method):
    """Split `payload` into <=block_size .ucas blocks, Oodle-compressing each so a
    patched mesh does not bloat the container (uncompressed it can double it).

    Every compressed block is verified to round-trip -- decompress back to the
    exact original -- before it is used. Anything that has no compressor, does
    not shrink, or does not round-trip is stored raw (method 0), so the output is
    always valid whatever the DLL does.
    """
    out = []
    for k in range(0, len(payload), block_size):
        raw = payload[k:k + block_size]
        comp = iostore.oodle_compress(raw) if comp_method else None
        ok = comp is not None and len(comp) < len(raw)
        if ok:
            try:
                ok = iostore.oodle_decompress(comp, len(raw)) == raw
            except Exception:
                ok = False
        out.append((comp, len(raw), comp_method) if ok else (raw, len(raw), 0))
    return out


def patch_mod(name, utoc_path):
    """
    Convert every skeletal mesh in one mod and rewrite its container.

    Returns True if anything changed, False if the mod was already converted.
    Raises if a mesh cannot be parsed -- in which case nothing is written, so a
    mod is either fully converted or left exactly as it was.
    """
    toc = iostore.Toc(utoc_path)
    base = os.path.splitext(os.path.basename(utoc_path))[0]
    src_dir = os.path.dirname(utoc_path)

    # --- Convert every package that needs it.
    pkg_indices = [i for i in sorted(toc.paths)
                   if toc.paths[i].endswith(".uasset")]
    print(f"    scanning {len(pkg_indices)} packages")
    new_data = {}
    size_deltas = {}
    for i in pkg_indices:
        data = toc.read(i)
        try:
            pkg = zen.ZenPackage(data)
        except Exception:
            continue
        if not any(e["cls"] == skm.SKELETAL_MESH for e in pkg.exports):
            continue

        patched, removed, reports = patch_package(data)
        if removed:
            new_data[i] = patched
            size_deltas[toc.package_id(i)] = removed
            for export_name, rep in reports:
                if rep.get("changed"):
                    print(f"    {toc.paths[i]} :: {export_name}")
                    print(f"      {rep['n_sections']} sections, "
                          f"{rep['n_bones']} bones, "
                          f"removed {rep['bytes_removed']:,} bytes "
                          f"({len(data):,} -> {len(patched):,})")

    if not new_data:
        print("    nothing to fix (already new-format)")
        return False

    # --- Back up before writing anything.
    backup = os.path.abspath(os.path.join(BACKUP_DIR, name))
    os.makedirs(backup, exist_ok=True)
    to_copy = []
    for ext in (".utoc", ".ucas", ".pak"):
        src = os.path.join(src_dir, base + ext)
        dst = os.path.join(backup, base + ext)
        if os.path.exists(src) and not os.path.exists(dst):
            to_copy.append((src, dst))
    if to_copy:
        mb = sum(os.path.getsize(s) for s, _ in to_copy) / (1024 * 1024)
        print(f"    backing up originals ({mb:,.0f} MB)")
        for src, dst in to_copy:
            shutil.copy(src, dst)

    # --- Rebuild the container.
    header_index = next(i for i in range(toc.n)
                        if toc.chunk_type(i) == 10)
    new_data[header_index] = rebuild_header(toc.read(header_index), size_deltas)

    print(f"    rebuilding container ({toc.n} chunks)")
    progress = sys.stdout.isatty()
    # Oodle's index in THIS container's method table -- a wrong index would make
    # the game misdecode our blocks. No Oodle in the table -> store raw.
    comp_method = next((m for m, method_name in enumerate(toc.methods)
                        if method_name.lower() == "oodle"), None)
    if comp_method is None and len(toc.methods) > 1:
        print(f"    note: container compresses with "
              f"{', '.join(toc.methods[1:])}, not Oodle -- "
              "storing patched chunks uncompressed")
    ucas_in = open(os.path.join(src_dir, base + ".ucas"), "rb")
    chunks = []
    new_paths = []
    for i in range(toc.n):
        if progress and i % 25 == 0:
            print(f"\r    reading chunk {i}/{toc.n}...", end="", flush=True)
        if i in new_data:
            payload = new_data[i]
            blocks = _pack_blocks(payload, toc.block_size, comp_method)
            size = len(payload)
        else:
            # Untouched chunk: reuse its compressed blocks as-is. build_metas_from
            # reuses the source checksum row, so its uncompressed bytes are never
            # needed -- skipping this decompress is the main speedup.
            offset, length = toc.offlen[i]
            b = offset // toc.block_size
            remaining = length
            blocks = []
            while remaining > 0:
                pos, csize, usize, method = toc.blocks[b]
                ucas_in.seek(pos)
                blocks.append((ucas_in.read(csize), usize, method))
                remaining -= usize
                b += 1
            size = length
        chunks.append(dict(id=toc.chunk_ids[i], blocks=blocks, size=size))
        if i in toc.paths:
            new_paths.append((toc.paths[i], len(chunks) - 1))

    if progress:
        print("\r" + " " * 40 + "\r", end="", flush=True)

    directory = dirindex.build_dir_index(toc.mount, new_paths)
    body, ucas, offlen, block_table = writer.build_container(
        toc, chunks, toc.block_size)
    head = writer.build_toc_header(toc, len(chunks), len(block_table),
                                   len(directory), toc.block_size)
    metas = writer.build_metas_from(toc, new_data)

    print(f"    writing {len(ucas) / (1024 * 1024):,.0f} MB to disk...", flush=True)
    with open(os.path.join(src_dir, base + ".utoc"), "wb") as f:
        f.write(head + bytes(body) + directory + metas)
    with open(os.path.join(src_dir, base + ".ucas"), "wb") as f:
        f.write(ucas)

    print(f"    written; .ucas now {len(ucas):,} bytes  (backup in backups/{name}/)")
    return True


def restore(name, utoc_path):
    """
    Put a mod back from ./backups/<name>/. Returns True if files were restored.
    """
    base = os.path.splitext(os.path.basename(utoc_path))[0]
    src_dir = os.path.dirname(utoc_path)
    backup = os.path.abspath(os.path.join(BACKUP_DIR, name))
    if not os.path.isdir(backup):
        print("    no backup found")
        return False
    n = 0
    for ext in (".utoc", ".ucas", ".pak"):
        b = os.path.join(backup, base + ext)
        if os.path.exists(b):
            shutil.copy(b, os.path.join(src_dir, base + ext))
            n += 1
    print(f"    restored {n} file(s) from backup")
    return True


# The outfit menu itself. It is a framework mod rather than a costume, and its
# state matters separately -- a costume mod is useless without it.
DRESSCODE = "Dresscode"

MARK = {"needs_fix": "[!!]", "patched": "[ok]", "none": "[--]", "error": "[??]"}


def mod_status(utoc):
    """Summarise one mod as (state, n_meshes, detail).

    state is one of: needs_fix, patched, none, error
    """
    try:
        _, found = scan(utoc)
    except Exception as ex:
        return "error", 0, f"{type(ex).__name__}: {ex}"
    if not found:
        return "none", 0, ""
    bad = [f for f in found if "error" in f]
    if bad:
        return "error", len(found), bad[0]["error"]
    if any(f["needs_fix"] for f in found):
        return "needs_fix", len(found), ""
    return "patched", len(found), ""


def _plural(n):
    return "es" if n != 1 else ""


def _wrap(items, width=66):
    """Pack a list of names into comma-separated lines."""
    out, line = [], ""
    for word in items:
        candidate = f"{line}, {word}" if line else word
        if len(candidate) > width and line:
            out.append(line + ",")
            line = word
        else:
            line = candidate
    if line:
        out.append(line)
    return out


def show_list(mods, debug=False):
    """
    Print the status of every installed mod.

    Dresscode is reported separately: it is the menu framework rather than a
    costume, and a missing one is worth flagging on its own.
    """
    print()
    for line in config.describe():
        print(line)
    for path in config.other_oodles():
        print(f"         also found:  {path}")
    print(f"  Mods   :            {config.MODS_DIR}")
    if getattr(config, "MODS_PAKS_DIR", "") and os.path.isdir(config.MODS_PAKS_DIR):
        print(f"  ~mods  :            {config.MODS_PAKS_DIR}")
    print()

    # Reading a mod decompresses its meshes -- slow with a big library, so show a
    # counter. The isatty guard keeps piped/redirected output clean.
    total = len(mods)
    progress = sys.stdout.isatty()
    results = {}
    for idx, (name, utoc) in enumerate(mods.items(), 1):
        if progress:
            print(f"\r  reading {idx}/{total}  {name[:40]:<40}", end="", flush=True)
        results[name] = mod_status(utoc)
    if progress:
        print("\r" + " " * 62 + "\r", end="", flush=True)

    # ---- Dresscode, on its own -----------------------------------------
    print("  Dresscode  (the base mod, by YIISx)")
    if DRESSCODE not in results:
        print("    [!!]  NOT INSTALLED")
        print("          Costume mods have no menu without it. Install Dresscode")
        print("          from its author first, then run this again.")
    else:
        # Dresscode ships its own official V1.005 build, so this tool never
        # patches it and makes no claim about its format.
        print("    [ok]  installed -- not patched by this tool")
        print("          If Dresscode itself crashes, get the author's official")
        print("          V1.005 release.")

    # ---- everything else ------------------------------------------------
    others = {k: v for k, v in results.items() if k != DRESSCODE}
    withmesh = {k: v for k, v in others.items() if v[0] in ("needs_fix", "patched")}
    errored = {k: v for k, v in others.items() if v[0] == "error"}
    nomesh = sorted(k for k, v in others.items() if v[0] == "none")

    if withmesh:
        print()
        print("  Mods with character meshes")
        width = max(len(k) for k in withmesh) + 2
        for name in sorted(withmesh):
            state, n, _ = withmesh[name]
            label = "needs patching" if state == "needs_fix" else "patched"
            src = "~mods" if mod_source(mods[name]) == "paks" else "Mods"
            print(f"    {MARK[state]}  {name:<{width}} {label:<15} "
                  f"{n} mesh{_plural(n)}  ({src})")

    if errored:
        print()
        print("  Could not read")
        for name in sorted(errored):
            print(f"    [??]  {name}: {errored[name][2]}")

    if nomesh:
        print()
        print("  No character meshes -- unaffected by V1.005")
        for chunk in _wrap(nomesh):
            print(f"    {chunk}")

    # ---- summary ---------------------------------------------------------
    # Dresscode is excluded -- it has an official update and is not patched here.
    need = sorted(k for k, v in results.items()
                  if v[0] == "needs_fix" and k != DRESSCODE)
    done = [k for k, v in results.items() if v[0] == "patched" and k != DRESSCODE]
    print()
    if need:
        s = "s" if len(need) != 1 else ""
        verb = "" if len(need) != 1 else "s"
        print(f"  {len(need)} mod{s} need{verb} patching:  {', '.join(need)}")
        print("  Run:  python patch.py --all")
    else:
        s = "s" if len(done) != 1 else ""
        print(f"  Nothing to do -- {len(done)} mod{s} already patched.")
    print()

    if debug:
        print("  --- debug: per-mesh detail ---")
        for name, utoc in mods.items():
            try:
                _, found = scan(utoc)
            except Exception as ex:
                print(f"    {name}: could not read -- {ex}")
                continue
            for f in found:
                if "error" in f:
                    print(f"    {name}: {f['path']} :: ERROR {f['error']}")
                else:
                    fmt = "old format" if f["needs_fix"] else "new format"
                    print(f"    {name}: {f['path']} :: {f['export']}")
                    print(f"        {f['n_lods']} LOD, {f['n_sections']} sections, "
                          f"{f['size']:,} bytes, {fmt}")
        print()


def _owns_console():
    """
    True when this process created the console window it is printing to --
    i.e. it was double-clicked rather than run from an existing terminal.

    Windows reports how many processes are attached to the console. If we are
    the only one, the window was created for us and will vanish the moment we
    exit, so the user needs a chance to read the output. Run from PowerShell or
    cmd, the shell is attached too, and pausing would just be an annoyance.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        buf = (ctypes.c_uint * 8)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
        return n <= 1
    except Exception:
        return False


def _finish(summary, argv):
    """Print the closing summary and hold the window open if we own it."""
    print()
    for line in summary:
        print(line)
    print()

    if "--no-pause" in argv:
        return
    if "--pause" in argv or _owns_console():
        try:
            input("Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass


def main(argv):
    """Run the requested action. Returns a process exit code."""
    mods = find_mods()
    if not mods:
        print("No mods found under", config.MODS_DIR)
        print("                or", config.MODS_PAKS_DIR)
        return 1

    want_all = "--all" in argv
    do_restore = "--restore" in argv
    listing = "--list" in argv
    debug = "--debug" in argv or "--verbose" in argv or "-v" in argv
    named = [a for a in argv if not a.startswith("-")]

    if listing:
        show_list(mods, debug)
        return 0

    targets = mods if want_all else {k: v for k, v in mods.items() if k in named}
    if not targets:
        print("Nothing selected. Use --list, --all, or name a mod.")
        print("Installed mods:", ", ".join(mods))
        return 1

    print()
    changed, unchanged, failed = [], [], []
    for name, utoc in targets.items():
        print(name)
        if name == DRESSCODE and not do_restore:
            print("    skipped -- Dresscode has an official V1.005 update; install")
            print("    it from its author instead of patching it here.")
            continue
        if do_restore:
            if restore(name, utoc):
                changed.append(name)
            else:
                unchanged.append(name)
            continue
        try:
            if patch_mod(name, utoc):
                changed.append(name)
            else:
                unchanged.append(name)
        except Exception as ex:
            print(f"    FAILED: {type(ex).__name__}: {ex}")
            failed.append(name)

    verb = "Restored" if do_restore else "Patched"
    summary = []
    if failed:
        summary.append(f"  {verb} {len(changed)}, skipped {len(unchanged)}, "
                       f"FAILED {len(failed)}: {', '.join(failed)}")
        summary.append("  The mods that failed were left untouched.")
    else:
        summary.append(f"  {verb} {len(changed)} mod"
                       f"{'s' if len(changed) != 1 else ''}"
                       + (f", skipped {len(unchanged)} already done."
                          if unchanged else "."))
        summary.append("  Done. Start the game and check your outfits.")

    _finish(summary, argv)
    return 1 if failed else 0


def startup():
    """
    Resolve everything needed before running, prompting if the Oodle library is
    the only thing missing.

    Any other problem (no game folder, a wrong path) is reported and we stop --
    those need a decision from the user, not a file.
    """
    problems = config.check()
    if not problems:
        return True

    missing_oodle = not config.OODLE_DLL
    others = [p for p in problems if "Oodle" not in p]

    if others:
        print()
        for p in others:
            print("  " + p)
        print()
        return False

    if missing_oodle and sys.stdin is not None and sys.stdin.isatty():
        import oodle_setup
        print()
        print("  Could not find an Oodle library (oo2core_*_win64.dll).")
        here = os.path.dirname(os.path.abspath(__file__))
        got = oodle_setup.prompt_for_oodle(here)
        if got:
            config.OODLE_DLL = got
            return not config.check()

    print()
    for p in problems:
        print("  " + p)
    print()
    return False


if __name__ == "__main__":
    if not startup():
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
