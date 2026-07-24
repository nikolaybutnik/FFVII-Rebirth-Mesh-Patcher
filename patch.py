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

By default the game is found automatically and its installed mods are patched in
place. To work on mods that are not installed -- e.g. to prepare a fixed build to
send on -- point the tool at any folder instead:

    python patch.py --path "D:\\mods"            list what is in that folder
    python patch.py --path "D:\\mods" --all      patch it all, in place
    python patch.py --path "D:\\mods" MyMod      patch just one
    python patch.py --path "D:\\mods" --out "D:\\send"   patched copies to --out,
                                                          originals left untouched

--path takes only the Oodle library, not the game, so it works on a machine
without FFVII Rebirth installed. A folder given as a bare argument (or dropped
onto patch.py) is treated the same as --path.

A mod's .utoc/.ucas/.pak names are never changed -- the loader (Dresscode) keys
off them, so a rename makes the mod undetectable. Originals are copied to
./backups/<ModName>/ before an in-place write; --out writes the patched triple
(same names) into another folder instead, taking no backup. The game and the
Oodle library are located automatically; see config.py.

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
import deps
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
    beneath ~mods, and some users nest each mod in its own subfolder. Skips our
    _patch_backups folders so backed-up originals don't resurface as mods."""
    root = os.path.abspath(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "_patch_backups"]
        if dirpath[len(root):].count(os.sep) >= max_depth:
            dirnames[:] = []
        for f in filenames:
            if f.endswith(".utoc"):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def _add_loader_mods(add, mods_dir):
    """End\\Mods layout: one folder per mod, keyed by the FOLDER name -- the
    handle the SKIP/Dresscode rules match on."""
    if not os.path.isdir(mods_dir):
        return
    for name in sorted(os.listdir(mods_dir)):
        if name in SKIP or name == _SELF:
            continue
        d = os.path.join(mods_dir, name, "Content", "Paks", "WindowsNoEditor")
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".utoc"):
                add(name, os.path.join(d, f))


def _add_loose_paks(add, paks):
    """Loose-pak folder (~mods, or any --path folder): one .utoc per mod, keyed
    by its .utoc stem."""
    for utoc in _find_pak_utocs(paks):
        add(os.path.splitext(os.path.basename(utoc))[0], utoc)


def _add_one_source(add, source):
    """Add the mods under a single dropped/`--path` folder (or .utoc). The
    game's own mod folders are discovered library-style, so their names and
    skips match the installed view."""
    src = os.path.abspath(source)
    if _game_present():
        mods_dir = os.path.abspath(config.MODS_DIR)
        if src == mods_dir:
            _add_loader_mods(add, config.MODS_DIR)
            return
        try:
            rel = os.path.relpath(src, mods_dir)
        except ValueError:              # different drive -> not under Mods
            rel = ".."
        if rel != ".." and not rel.startswith(".." + os.sep):
            # Inside Mods: key by the owning mod folder, like the library view,
            # so backups match what --restore looks for and skips still apply.
            name = rel.split(os.sep)[0]
            if name not in SKIP and name != _SELF:
                d = os.path.join(mods_dir, name,
                                 "Content", "Paks", "WindowsNoEditor")
                if os.path.isdir(d):
                    for f in sorted(os.listdir(d)):
                        if f.endswith(".utoc"):
                            add(name, os.path.join(d, f))
            return
        paks = getattr(config, "MODS_PAKS_DIR", "")
        if paks and src == os.path.abspath(paks):
            _add_loose_paks(add, paks)
            return
    if src.lower().endswith(".utoc"):
        if os.path.isfile(src):
            add(os.path.splitext(os.path.basename(src))[0], src)
    else:
        _add_loose_paks(add, src)


def find_mods(sources=None):
    """Return {mod_name: utoc_path} for every mod to consider.

    Default (library) mode -- sources empty -- finds installed mods in the two
    places the game loads them, both treated the same once found:

      End\\Mods\\<name>\\Content\\Paks\\WindowsNoEditor\\   the FF7RML layout,
                                                            one folder per mod
      End\\Content\\Paks\\~mods\\                           Unreal's loose-pak
                                                            folder, one .utoc
                                                            per mod

    Folder mode -- sources is a list of paths (dropped folders, or --path) --
    scans ONLY those, merged, replacing the default locations.

    Names key the backup folders, so a clash would make one mod's backup overwrite
    another's -- add() keeps them unique.
    """
    out = {}

    def add(name, utoc):
        key, n = name, 2
        while key in out and out[key] != utoc:
            key = f"{name} ({n})"
            n += 1
        out[key] = utoc

    if sources:
        for source in sources:
            _add_one_source(add, source)
        return out

    # --- Library mode: the game's two mod locations ----------------------
    _add_loader_mods(add, config.MODS_DIR)
    paks = getattr(config, "MODS_PAKS_DIR", "")
    if paks and os.path.isdir(paks):
        _add_loose_paks(add, paks)

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
    # No file index but packages inside: we cannot see what they are, so never
    # claim "unaffected".
    if not toc.paths and any(toc.chunk_type(i) == 2 for i in range(toc.n)):
        return toc, [dict(chunk=-1, path="", export="", size=0,
                          error="this mod has no list of its own files, so "
                                "this tool cannot see what is inside -- "
                                "please report this mod")]
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
                    needs = meshfix.old_format(
                        payload, lod["sections_at"], lod["n_sections"])
                    found.append(dict(chunk=i, path=toc.paths[i], export=e["name"],
                                      size=e["size"], n_lods=lod["n_lods"],
                                      n_sections=lod["n_sections"],
                                      needs_fix=needs))
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


def _mod_rel(utoc_path):
    """The mod's own on-disk wrapping (loader mods live under
    Content\\Paks\\WindowsNoEditor), mirrored into its backup so the backup
    stands alone."""
    tail = os.path.join("Content", "Paks", "WindowsNoEditor")
    d = os.path.dirname(os.path.abspath(utoc_path))
    return tail if d.lower().endswith(tail.lower()) else ""


def patch_mod(name, utoc_path, out_dir=None, backup_dir=None, no_backup=False):
    """
    Convert every skeletal mesh in one mod and rewrite its container.

    The mod's .utoc/.ucas/.pak names are NEVER changed -- the loader keys off
    them, so a rename makes the mod vanish. Default is in place: originals are
    first copied to backup_dir/<name>/ (mirroring the mod's structure, see
    _mod_rel) and --restore undoes it from the same root. out_dir writes the
    triple elsewhere instead, original untouched, unchanged .pak copied along
    so the result loads. no_backup skips the backup when patching a throwaway
    copy -- the untouched source is the backup; see _patch_copy.

    Returns True if anything changed, False if the mod was already converted.
    Raises if a mesh cannot be parsed -- in which case nothing is written, so a
    mod is either fully converted or left exactly as it was.
    """
    toc = iostore.Toc(utoc_path)
    if not toc.paths and any(toc.chunk_type(i) == 2 for i in range(toc.n)):
        raise RuntimeError("this mod has no list of its own files, so this "
                           "tool cannot see inside it to patch it -- please "
                           "report this mod")
    base = os.path.splitext(os.path.basename(utoc_path))[0]
    src_dir = os.path.dirname(utoc_path)
    dst_dir = os.path.abspath(out_dir) if out_dir else src_dir
    backup_dir = backup_dir or BACKUP_DIR
    in_place = os.path.abspath(dst_dir) == os.path.abspath(src_dir)

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

    # Back up before writing anything (in-place only).
    if not in_place:
        os.makedirs(dst_dir, exist_ok=True)
    elif no_backup:
        pass                            # patching a throwaway copy -- see above
    else:
        backup = os.path.abspath(os.path.join(backup_dir, name, _mod_rel(utoc_path)))
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
    with open(os.path.join(dst_dir, base + ".utoc"), "wb") as f:
        f.write(head + bytes(body) + directory + metas)
    with open(os.path.join(dst_dir, base + ".ucas"), "wb") as f:
        f.write(ucas)
    # The .pak is never rewritten, but the game loads a mod as a triple -- copy
    # it across whenever the original is not being overwritten in place.
    if not in_place:
        pak_src = os.path.join(src_dir, base + ".pak")
        if os.path.exists(pak_src):
            shutil.copy(pak_src, os.path.join(dst_dir, base + ".pak"))

    if not in_place:
        print(f"    written {base}.utoc/.ucas/.pak  in  {dst_dir}")
    elif no_backup:
        print(f"    written; .ucas now {len(ucas):,} bytes")
    else:
        # The full backup path is stated once, in the closing summary.
        print(f"    written; .ucas now {len(ucas):,} bytes  (original backed up)")
    return True


def restore(name, utoc_path, backup_dir=None):
    """
    Put a mod back from its backup. backup_dir must be the same root patch_mod
    wrote to -- the central ./backups for installed mods, or the folder-local one
    for a mod patched via --path. Returns True if files were restored.
    """
    base = os.path.splitext(os.path.basename(utoc_path))[0]
    src_dir = os.path.dirname(utoc_path)
    # Backups mirror the mod's structure; fall back to the flat root for backups
    # written by older versions.
    root = os.path.abspath(os.path.join(backup_dir or BACKUP_DIR, name))
    structured = os.path.join(root, _mod_rel(utoc_path))
    backup = structured if os.path.isdir(structured) else root
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


_avail = None


def _game_present():
    """Whether a real game install was located -- folder mode runs without one,
    so anything install-relative (companion-mod warnings, the game's own mod
    folders) must check first."""
    return bool(config.GAME_DIR) and os.path.isdir(config.GAME_PAKS)


def _missing_reqs(utoc):
    """Known companion mods this mod needs but the user has not installed."""
    global _avail
    if not _game_present():
        return []
    try:
        if _avail is None:
            _avail = deps.installed_ids()
        return deps.missing_requirements(utoc, _avail)[0]
    except Exception:
        return []


def _plural(n):
    return "es" if n != 1 else ""


def show_list(mods, debug=False, sources=None):
    """
    Print the status of every mod found.

    Dresscode is reported separately: it is the menu framework rather than a
    costume, and a missing one is worth flagging on its own. It only makes
    sense where the loader folder is in scope -- library mode, or a drop of
    Mods itself -- so folder mode otherwise leaves it (and the install-specific
    header) out.
    """
    sources = sources or []
    folder_mode = bool(sources)
    show_dresscode = not sources or any(_is_loader_root(s) for s in sources)

    def src_tag(utoc):
        """The '(Mods)'/'(~mods)' suffix; noise in folder mode."""
        if folder_mode:
            return ""
        return "  (~mods)" if mod_source(utoc) == "paks" else "  (Mods)"

    print()
    for line in config.describe():
        print(line)
    for path in config.other_oodles():
        print(f"         also found:  {path}")
    if folder_mode:
        label = "Source" if len(sources) == 1 else "Sources"
        print(f"  {label:<7}:            {os.path.abspath(sources[0])}")
        for s in sources[1:]:
            print(f"                      {os.path.abspath(s)}")
    else:
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

    # ---- Dresscode, on its own -------------------------------------------
    if show_dresscode:
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
            print(f"    {MARK[state]}  {name:<{width}} {label:<15} "
                  f"{n} mesh{_plural(n)}{src_tag(mods[name])}")

    if errored:
        print()
        print("  Could not read")
        for name in sorted(errored):
            print(f"    [??]  {name}: {errored[name][2]}")

    if nomesh:
        print()
        print("  No character meshes -- unaffected by V1.005")
        width = max(len(k) for k in nomesh) + 2
        for name in nomesh:
            print(f"    [--]  {name:<{width}}{src_tag(mods[name])}")

    # ---- missing companion mods -----------------------------------------
    reqs = {name: r for name, utoc in mods.items()
            if (r := _missing_reqs(utoc))}
    if reqs:
        print()
        print("  Missing required files -- these mods reference another mod that")
        print("  is NOT installed. They will load with grey-checker textures.")
        for name in sorted(reqs):
            for r in reqs[name]:
                print(f"    [!!]  {name}  needs {r}")
        print("          Patching cannot fix this -- install the missing mod")
        print("          (see the Requirements on the mod's download page).")

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
        scope = "".join(f' --path "{os.path.abspath(s)}"' for s in sources)
        print(f"  Run:  python patch.py{scope} --all")
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
        print("  --- debug: unresolved package imports ---")
        avail = deps.installed_ids()
        for name, utoc in mods.items():
            known, unknown = deps.missing_requirements(utoc, avail)
            if known or unknown:
                ids = ", ".join(f"{i:#x}" for i in sorted(unknown))
                print(f"    {name}: needs {known or 'nothing known'}; "
                      f"other unresolved: {ids or 'none'}")
        print()


# Names (lowercase) of interactive shells / terminals. If one of these is
# sharing our console, we were launched from it rather than owning the window.
_SHELLS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe",
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


def _owns_console():
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
    return not any(n in _SHELLS for n in names)


def _finish(summary):
    """Print the closing summary."""
    print()
    for line in summary:
        print(line)
    print()


# Set once a menu has handled the final keypress, so the end-of-run pause
# does not demand a second Enter.
_INTERACTED = False


def _pause_before_exit(argv):
    """Hold the window open when we own it, so double-clickers can read the
    output. Runs on EVERY exit -- listing, errors, "nothing selected" -- not
    just after patching."""
    if _INTERACTED or "--no-pause" in argv:
        return
    if "--pause" in argv or _owns_console():
        try:
            input("Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass


def _wrapper_dir(source):
    """The "Patched Mods" folder placed beside a dropped folder to hold its
    patched copies."""
    return os.path.join(os.path.dirname(os.path.abspath(source.rstrip("\\/"))),
                        "Patched Mods")


def _folder_menu(sources):
    """One y/N after a drop: patch every mod, copies to "Patched Mods" beside
    each source, originals untouched. Custom in/out locations are the CLI's
    job. Returns an exit code."""
    global _INTERACTED
    print("  ----------------------------------------------------------------")
    n = len(sources)
    where = "this folder" if n == 1 else f"these {n} folders"
    print(f"  This will patch every mod in {where} and save the patched")
    print("  copies -- originals untouched -- to a \"Patched Mods\" folder beside")
    print("  " + ("it:" if n == 1 else "each:"))
    for s in sources:
        print(f"      {_wrapper_dir(s)}{os.sep}")
    try:
        ans = input("  Proceed?  [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _INTERACTED = True
        return 0
    if ans in ("y", "yes"):
        codes = [_patch_copy(s) for s in sources]
        return max(codes) if codes else 0
    _INTERACTED = True
    print("  Nothing changed.")
    return 0


def _patch_copy(source):
    """Patch a COPY into the "Patched Mods" wrapper, keeping the mod's EXACT
    folder and file names -- the loader keys off them, and the wrapper (which
    the game never reads) is the only added name. A folder is copied whole and
    patched in place with no backup (the untouched source is the backup); a
    lone .utoc goes through --out. Returns an exit code."""
    global _INTERACTED
    src = os.path.abspath(source.rstrip("\\/"))
    wrapper = _wrapper_dir(source)

    if os.path.isfile(src):                     # a lone .utoc -- exact name kept
        return main(["--path", src, "--out", wrapper, "--all"])

    dst = os.path.join(wrapper, os.path.basename(src))
    print(f"  Copying to {dst} ...")
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except Exception as ex:
        print(f"  Could not copy: {ex}")
        _INTERACTED = True
        return 0
    return main(["--path", dst, "--all", "--no-backup"])


def _confirm_game_folder(sources):
    """Drop was the game's own Mods/~mods (or inside them): a copy beside the
    original would just load twice, so confirm a straight in-place patch with
    central backups. Returns an exit code."""
    global _INTERACTED
    print("  ----------------------------------------------------------------")
    print("  That is inside your game install -- these are your installed mods.")
    print("  This patches the ones that need it, in place. Your originals are")
    print("  backed up first, to:")
    print(f"      {os.path.abspath(BACKUP_DIR)}{os.sep}")
    try:
        ans = input("  Proceed?  [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _INTERACTED = True
        return 0
    if ans in ("y", "yes"):
        argv = []
        for s in sources:
            argv += ["--path", s]
        argv.append("--all")
        return main(argv)
    _INTERACTED = True
    print("  Nothing changed.")
    return 0


def _parse_args(argv):
    """Split argv into (sources, out, names, flags).

    --path/--out take a value, as the next token or glued on (--path=DIR).
    Bare positionals that resolve to an existing folder or .utoc are sources
    too, so dropping one or several folders onto patch.py lands in folder mode
    without typing --path. Other positionals are mod names.
    """
    sources, names, flags = [], [], []
    out = None
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        key, eq, val = a.partition("=")
        if key in ("--path", "--out"):
            if not eq:                              # value is the next token
                i += 1
                val = argv[i] if i < n else ""
            if key == "--path":
                sources.append(val)
            else:
                out = val
        elif a.startswith("-"):
            flags.append(a)
        elif os.path.isdir(a) or a.lower().endswith(".utoc"):
            sources.append(a)
        else:
            names.append(a)
        i += 1
    return sources, out, names, flags


def _game_mod_dirs():
    """The game's own mod folders, absolute; empty when no game is installed."""
    if not _game_present():
        return []
    dirs = [os.path.abspath(config.MODS_DIR)]
    paks = getattr(config, "MODS_PAKS_DIR", "")
    if paks:
        dirs.append(os.path.abspath(paks))
    return dirs


def _under_game_mods(source):
    """True when `source` is one of the game's mod folders or inside one --
    installed mods, handled in place with central backups."""
    if source is None:
        return False
    p = os.path.abspath(source)
    for d in _game_mod_dirs():
        try:
            if os.path.commonpath([p, d]) == d:
                return True
        except ValueError:              # different drive -> not under it
            pass
    return False


def _is_loader_root(source):
    """True when `source` IS the game's Mods (loader) folder -- the only place
    Dresscode lives, so the Dresscode note belongs only here. Never ~mods."""
    return (source is not None and _game_present()
            and os.path.abspath(source) == os.path.abspath(config.MODS_DIR))


def _all_under_game(sources):
    """True when every dropped source is installed mods -- the whole drop is
    handled in place rather than via the copy flow."""
    return bool(sources) and all(_under_game_mods(s) for s in sources)


def _backup_root(sources):
    """Where in-place backups for this run live: central ./backups for
    installed mods, a _patch_backups inside the folder otherwise -- so folder
    mods never collide with same-named installed ones."""
    if not sources or _all_under_game(sources):
        return BACKUP_DIR
    root = os.path.abspath(sources[0])
    if root.lower().endswith(".utoc"):
        root = os.path.dirname(root)
    return os.path.join(root, "_patch_backups")


def main(argv):
    """Run the requested action. Returns a process exit code."""
    sources, out_dir, named, flags = _parse_args(argv)

    mods = find_mods(sources)
    if not mods:
        if sources:
            print("No mods (.utoc) found under:")
            for s in sources:
                print("   ", os.path.abspath(s))
        else:
            print("No mods found under", config.MODS_DIR)
            print("                or", config.MODS_PAKS_DIR)
        return 1

    want_all = "--all" in flags
    do_restore = "--restore" in flags
    listing = "--list" in flags
    no_backup = "--no-backup" in flags
    debug = any(f in flags for f in ("--debug", "--verbose", "-v"))

    # A source with no action is a request to see what is there.
    if sources and not (want_all or do_restore or named):
        listing = True

    if listing:
        show_list(mods, debug, sources=sources)
        # A drop owns its window, so a bare listing would dead-end at the exit
        # pause -- offer the follow-up: in-place confirm for installed mods,
        # the copy flow for anything else.
        if sources and not (want_all or do_restore or named) and _owns_console():
            if _all_under_game(sources):
                return _confirm_game_folder(sources)
            return _folder_menu(sources)
        return 0

    targets = mods if want_all else {k: v for k, v in mods.items() if k in named}
    unknown = [n for n in named if n not in mods]
    if unknown:
        print("No mod called:", ", ".join(unknown), " (check the spelling)")
    if not targets:
        print("Nothing selected. Use --list, --all, or name a mod.")
        print("Installed mods:", ", ".join(mods))
        return 1

    backup_base = _backup_root(sources)
    print()
    changed, unchanged, failed = [], [], []
    for name, utoc in targets.items():
        print(name)
        if name == DRESSCODE and not do_restore:
            print("    skipped -- Dresscode has an official V1.005 update; install")
            print("    it from its author instead of patching it here.")
            continue
        if do_restore:
            if restore(name, utoc, backup_base):
                changed.append(name)
            else:
                unchanged.append(name)
            continue
        try:
            if patch_mod(name, utoc, out_dir, backup_base, no_backup):
                changed.append(name)
            else:
                unchanged.append(name)
            for r in _missing_reqs(utoc):
                print(f"    note: this mod also needs {r}, which is not")
                print("    installed -- without it, textures show as grey checkers.")
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

    # Spell out where the originals are so the user knows what they can delete.
    if changed and not do_restore:
        summary.append("")
        if no_backup:
            if sources:
                summary.append("  Patched copy is ready (your original was left"
                               " untouched):")
                summary.append(f"      {os.path.abspath(sources[0])}{os.sep}")
            else:
                summary.append("  Patched in place; no backup was taken"
                               " (--no-backup).")
        elif out_dir:
            summary.append(f"  Patched copies written to  {os.path.abspath(out_dir)}")
            summary.append("  Your original files were left exactly as they were.")
        else:
            summary.append("  Your untouched originals are backed up in:")
            summary.append(f"    {os.path.abspath(backup_base)}{os.sep}"
                           "   (one folder per mod)")
            summary.append("  Keep it to undo later with --restore, or just delete")
            summary.append("  it once the game looks right -- your call.")

    if not failed:
        summary.append("")
        summary.append("  Done. Start the game and check your outfits.")

    _finish(summary)
    return 1 if failed else 0


def startup(require_game=True):
    """
    Resolve everything needed before running, prompting if the Oodle library is
    the only thing missing.

    Any other problem (no game folder, a wrong path) is reported and we stop --
    those need a decision from the user, not a file. In folder mode
    (require_game False) the game folder is not needed at all, so only Oodle is
    checked.
    """
    problems = config.check(require_game)
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
            return not config.check(require_game)

    print()
    for p in problems:
        print("  " + p)
    print()
    return False


if __name__ == "__main__":
    # Folder mode (--path or dropped folders) needs only Oodle, not the game.
    _sources = _parse_args(sys.argv[1:])[0]
    code = 0 if startup(require_game=not _sources) else 1
    if code == 0:
        code = main(sys.argv[1:])
    _pause_before_exit(sys.argv[1:])
    sys.exit(code)
