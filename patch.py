"""
patch.py -- FFVII Rebirth mesh patcher.

Fixes mods that were built before game patch V1.005 and no longer load. Works on
costume mods and pak mods -- anything containing a skeletal mesh. (Dresscode
itself now has an official V1.005 update, so it is no longer patched here.) Mods
are found in End\\Mods (the FF7RML loader) and in
End\\Content\\Paks\\~mods (paks the game loads directly); see find_mods.

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
onto patch.py) is treated the same as --path; a dropped .zip/.7z/.rar is
unpacked first, archives nested inside it included.

unpatch.py is this tool in REVERSE -- same flows, same flags -- for players
still on game version 1.004. It flips MODE to BACKWARD (see FORWARD/BACKWARD
below) and keeps its own backups folder; nothing else differs.

Names cut both ways. A mod's .utoc/.ucas/.pak names are never changed -- the
loader keys off them, so a rename makes the mod undetectable. A loader mod's
FOLDER is the opposite: Dresscode looks a mod up by folder name and ignores one
that does not match the .uplugin inside, so a dropped mod's folder is corrected
to match (see _fix_loader_names).

Originals are copied to ./backups/<ModName>/ before an in-place write; --out
writes the patched triple (same names) into another folder instead, taking no
backup. The game and the Oodle library are located automatically; see config.py.

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
import drops
import iostore
import meshfix
import repack
import skm
import zen

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

# The two directions this tool runs in. patch.py converts 1.004 mods up to the
# V1.005 mesh layout; unpatch.py sets MODE = BACKWARD (and its own BACKUP_DIR)
# and converts back down, for players still on the old game version. Everything
# else -- discovery, drops, backups, restore -- is shared and direction-blind.
FORWARD = dict(
    tool="patch.py", verb="Patched", menu_verb="patch",
    wrapper="Patched Mods", local_backups="_patch_backups",
    needs=meshfix.old_format, convert=meshfix.convert_payload,
    needs_label="needs patching", done_label="patched",
    already="nothing to fix (already new-format)",
    done_inside="already patched",
    dresscode_ok=["installed -- not patched by this tool "
                  "(it has its own official updates)"],
    dresscode_skip=["Dresscode has an official V1.005 update; install",
                    "it from its author instead of patching it here."],
)
BACKWARD = dict(
    tool="unpatch.py", verb="Unpatched", menu_verb="unpatch",
    wrapper="Unpatched Mods", local_backups="_unpatch_backups",
    needs=meshfix.new_format, convert=meshfix.unconvert_payload,
    needs_label="needs unpatching", done_label="1.004 already",
    already="nothing to do (already the old 1.004 format)",
    done_inside="already back to the old 1.004 format",
    dresscode_ok=["installed -- not touched by this tool "
                  "(use its author's release for your game version)"],
    dresscode_skip=["Dresscode is not converted here; for the 1.004",
                    "game install the author's original release."],
)
MODE = FORWARD

# Mods that are part of the loader framework, not content -- never touch these.
SKIP = {"FF7RML", "FF7RModMenu"}

# If this tool was dropped inside End\Mods\ it must not try to patch itself.
_SELF = os.path.basename(os.path.dirname(os.path.abspath(__file__)))


# Windows paths and mod names are case-insensitive, so every comparison of them
# must be too: ...\mods\ff7rml is the SAME folder as ...\Mods\FF7RML, and an
# exact match walks straight past the guards that keep the loader framework and
# Dresscode from being patched.
def _same_path(a, b):
    return (os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


def _path_under(path, root):
    """True when `path` is `root` or sits inside it."""
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:                  # different drive
        return False


def _is_skipped(name):
    """A loader-framework folder, or this tool itself -- never content."""
    return name.lower() in {s.lower() for s in SKIP} or name.lower() == _SELF.lower()


def _central_backup_dirs():
    """The central backup folders -- this tool's, its reverse twin's, and
    whatever BACKUP_DIR currently points at. People unzip the tool INSIDE
    ~mods, which puts these inside the scan: the backed-up original then
    lists as a mod needing patching and gets patched over -- the pristine
    copy destroyed by the very tool that made it (real 1.4.0 field report,
    'it also tries to patch and read its own backup folder')."""
    here = os.path.dirname(os.path.abspath(__file__))
    return {os.path.normcase(os.path.abspath(p))
            for p in (BACKUP_DIR,
                      os.path.join(here, "backups"),
                      os.path.join(here, "unpatch_backups"))}


def _find_pak_utocs(root, max_depth=5):
    """Every .utoc under `root`, depth-limited. The game loads paks recursively
    beneath ~mods, and some users nest each mod in its own subfolder. Skips our
    backup folders so backed-up originals don't resurface as mods."""
    root = os.path.abspath(root)
    central = _central_backup_dirs()
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in ("_patch_backups",
                                            "_unpatch_backups")
                       and os.path.normcase(os.path.join(dirpath, d))
                       not in central]
        if dirpath[len(root):].count(os.sep) >= max_depth:
            dirnames[:] = []
        for f in filenames:
            if f.lower().endswith(".utoc"):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def _add_loader_mods(add, mods_dir):
    """End\\Mods layout: one folder per mod, keyed by the FOLDER name -- the
    handle the SKIP/Dresscode rules match on."""
    if not os.path.isdir(mods_dir):
        return
    for name in sorted(os.listdir(mods_dir)):
        if _is_skipped(name):
            continue
        d = os.path.join(mods_dir, name, "Content", "Paks", "WindowsNoEditor")
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".utoc"):
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
        if _same_path(src, mods_dir):
            _add_loader_mods(add, config.MODS_DIR)
            return
        if _path_under(src, mods_dir):
            # Inside Mods: key by the owning mod folder under its real on-disk
            # name, whatever casing was typed, so the key matches the library
            # view -- which is what --restore looks for and what SKIP matches.
            name = os.path.relpath(src, mods_dir).split(os.sep)[0]
            name = next((a for a in os.listdir(mods_dir)
                         if a.lower() == name.lower()), name)
            if not _is_skipped(name):
                d = os.path.join(mods_dir, name,
                                 "Content", "Paks", "WindowsNoEditor")
                if os.path.isdir(d):
                    for f in sorted(os.listdir(d)):
                        if f.lower().endswith(".utoc"):
                            add(name, os.path.join(d, f))
            return
        paks = getattr(config, "MODS_PAKS_DIR", "")
        if paks and _same_path(src, paks):
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
    if paks and _path_under(utoc_path, paks):
        return "paks"
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
                    needs = MODE["needs"](
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
            error="files did not decode -- the Oodle DLL is likely the "
                  "wrong version for this game (need oo2core_6 or newer)"))
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
            # Convert only what the scan called out. The converter also
            # normalizes traits that are legal either way (the dup-vert
            # arrays), and running it unconditionally rewrote mods the
            # listing had just called [ok].
            payload = bytes(payloads[k])
            try:
                after, _info = skm.parse_head(payload, 0, len(payload),
                                              skm.NoNames(), verbose=False)
                lod = skm.parse_lod_header(payload, after)
                if not MODE["needs"](payload, lod["sections_at"],
                                     lod["n_sections"]):
                    continue
            except Exception:
                pass                # let the converter raise its clear error
            new_payload, report = MODE["convert"](payload)
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
    # Same folder written differently is still the same folder -- get this wrong
    # and we would rewrite the originals while reading them, with no backup.
    in_place = _same_path(dst_dir, src_dir)

    # --- Convert every package that needs it.
    pkg_indices = [i for i in sorted(toc.paths)
                   if toc.paths[i].endswith(".uasset")]
    print(f"    scanning {len(pkg_indices)} files")
    live = sys.stdout.isatty()

    def clear():
        if live:
            print("\r" + " " * 46 + "\r", end="", flush=True)

    new_data = {}
    size_deltas = {}
    for n, i in enumerate(pkg_indices):
        if live and n % 10 == 0:
            print(f"\r    scanning file {n + 1}/{len(pkg_indices)}...",
                  end="", flush=True)
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
                    delta = rep["bytes_removed"]
                    clear()
                    print(f"    fixed  {toc.paths[i]}  "
                          f"({'removed' if delta >= 0 else 'added'} "
                          f"{abs(delta):,} bytes)")
    clear()

    if not new_data:
        print(f"    {MODE['already']}")
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

    print("    rebuilding the mod file")
    repack.write(toc, new_data, dst_dir, base, src_dir,
                 copy_pak=not in_place)

    if not in_place:
        print(f"    written {base}.utoc/.ucas/.pak  in  {dst_dir}")
    elif no_backup:
        print("    written")
    else:
        # The full backup path is stated once, in the closing summary.
        print("    written  (original backed up)")
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


def _is_dresscode(name):
    return name.lower() == DRESSCODE.lower()


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
        if not any(_is_dresscode(k) for k in results):
            print("    [!!]  NOT INSTALLED")
            print("          Costume mods have no menu without it. Install Dresscode")
            print("          from its author first, then run this again.")
        else:
            # Dresscode ships its own official builds, so this tool never
            # converts it and makes no claim about its format.
            print(f"    [ok]  {MODE['dresscode_ok'][0]}")
            for line in MODE["dresscode_ok"][1:]:
                print(f"          {line}")

    # ---- everything else ------------------------------------------------
    others = {k: v for k, v in results.items() if not _is_dresscode(k)}
    withmesh = {k: v for k, v in others.items() if v[0] in ("needs_fix", "patched")}
    errored = {k: v for k, v in others.items() if v[0] == "error"}
    nomesh = sorted(k for k, v in others.items() if v[0] == "none")

    if withmesh:
        print()
        print("  Mods with character meshes")
        width = max(len(k) for k in withmesh) + 2
        for name in sorted(withmesh):
            state, n, _ = withmesh[name]
            label = (MODE["needs_label"] if state == "needs_fix"
                     else MODE["done_label"])
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
                  if v[0] == "needs_fix" and not _is_dresscode(k))
    done = [k for k, v in results.items()
            if v[0] == "patched" and not _is_dresscode(k)]
    print()
    if need:
        s = "s" if len(need) != 1 else ""
        verb = "" if len(need) != 1 else "s"
        what = MODE["needs_label"].split(" ", 1)[1]      # "patching"/"unpatching"
        print(f"  {len(need)} mod{s} need{verb} {what}:  {', '.join(need)}")
        scope = "".join(f' --path "{os.path.abspath(s)}"' for s in sources)
        print(f"  Run:  python {MODE['tool']}{scope} --all")
    else:
        s = "s" if len(done) != 1 else ""
        print(f"  Nothing to do -- {len(done)} mod{s} already "
              f"{'patched' if MODE is FORWARD else 'in the 1.004 format'}.")
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

    return bool(need)


# Console-ownership detection lives in lib/drops.py, shared with convert.py.
_owns_console = drops.owns_console


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
    """The "Patched Mods" (or "Unpatched Mods") folder placed beside a dropped
    folder (or zip) to hold its converted copies."""
    return os.path.join(os.path.dirname(os.path.abspath(source.rstrip("\\/"))),
                        MODE["wrapper"])


# Archive handling -- detection, extraction, nested unpacking -- lives in
# lib/drops.py, shared with convert.py. Aliased so call sites read the same.
_ARCHIVE_EXTS = drops.ARCHIVE_EXTS
_is_archive = drops.is_archive
_archives_in = drops.archives_in
_contains_archive = drops.contains_archive
_show_archive = drops.show_archive
_archive_summary = drops.archive_summary
_extract_archive = drops.extract_archive
_expand_archives = drops.expand_archives
_peek_line = drops.progress
_peek_done = drops.progress_done


# Archives unpacked to be looked inside, keyed by absolute path. Unpacking
# is the only way to see what a mod is, and answering "yes" would unpack the
# same archive again -- so the copy is kept and patched in place instead.
_UNPACKED = {}


def _unpack_to_look(arc):
    """
    An unpacked copy of `arc` in a temporary folder, or None if it will not
    open. Unpacked once per run: the scan reads it, and the patch that may
    follow moves this very copy into place rather than repeating the work.
    """
    key = os.path.normcase(os.path.abspath(arc))
    if key in _UNPACKED:
        return _UNPACKED[key]
    import tempfile
    dst = tempfile.mkdtemp(prefix="modscan-")
    try:
        _extract_archive(arc, dst)
        if _contains_archive(dst):
            _peek_done()        # nested unpacking prints lines of its own
        _expand_archives(dst)
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)
        _UNPACKED[key] = None
        return None
    _UNPACKED[key] = dst
    return dst


def _discard_unpacked():
    """Drop what the scan unpacked and nothing took over."""
    for dst in _UNPACKED.values():
        if dst:
            shutil.rmtree(dst, ignore_errors=True)
    _UNPACKED.clear()


def _archive_needs(arc):
    """
    Whether anything inside `arc` still needs work: True, False, or None
    when it cannot be looked into. None is NOT "nothing to do" -- an archive
    no tool here can open is offered as before, and the extraction attempt
    is what reports why.
    """
    dst = _unpack_to_look(arc)
    if dst is None:
        return None
    found = find_mods([dst])
    if not found:
        return None
    try:
        return any(mod_status(u)[0] == "needs_fix" for u in found.values())
    except Exception:
        return None


def _archive_covered(arc, mod_names):
    """True when every mod inside `arc` already appears in the scan -- the
    archive was extracted and handled on an earlier run, so offering it again
    would only duplicate mods the user already has."""
    names, inner = _archive_summary(arc)
    if inner or not names:
        return False
    have = [k.lower() for k in mod_names]
    return all(any(k == m.lower() or k.startswith(m.lower() + " (")
                   for k in have) for m in names)


def _dresscode_stem(folder):
    """The .uplugin stem inside `folder` -- the name Dresscode looks a loader mod
    up by, and thus the name the folder must have. None unless there is exactly
    one .uplugin, so plain pak folders and anything irregular are left alone."""
    try:
        ups = [f for f in os.listdir(folder) if f.lower().endswith(".uplugin")]
    except OSError:
        return None
    return os.path.splitext(ups[0])[0] if len(ups) == 1 else None


def _fix_loader_names(root):
    """Rename every loader mod folder under `root` to match its .uplugin.

    Dresscode looks a mod up by its folder name and ignores it if that does not
    equal the .uplugin inside; some authors ship a folder named for the download,
    so the mod never appears. Returns root's own path, which may itself have
    been renamed.
    """
    root = os.path.abspath(root)
    targets = []
    for dirpath, _dirnames, _filenames in os.walk(root):
        stem = _dresscode_stem(dirpath)
        if stem and os.path.basename(dirpath) != stem:
            targets.append((dirpath, stem))
    # Deepest first, so a parent rename never invalidates a pending child path.
    new_root = root
    for old, stem in sorted(targets, key=lambda t: -t[0].count(os.sep)):
        new = os.path.join(os.path.dirname(old), stem)
        if os.path.normcase(new) != os.path.normcase(old) and os.path.exists(new):
            print(f"  note: \"{os.path.basename(old)}\" should be named \"{stem}\""
                  " for Dresscode, but that name is taken -- left as is")
            continue
        os.rename(old, new)
        print(f"  named for Dresscode:  {os.path.basename(old)}  ->  {stem}")
        if os.path.normcase(old) == os.path.normcase(root):
            new_root = new
    return new_root


def _folder_menu(sources):
    """One y/N after a drop: patch every mod, copies to "Patched Mods" beside
    each source, originals untouched. Custom in/out locations are the CLI's
    job. Returns an exit code."""
    global _INTERACTED
    print("  ----------------------------------------------------------------")
    print(f"  This will {MODE['menu_verb']} every mod in what you dropped"
          " (archives are")
    print(f"  extracted first) and save the {MODE['menu_verb']}ed copies --"
          " originals")
    print(f"  untouched -- to a \"{MODE['wrapper']}\" folder beside "
          + ("it:" if len(sources) == 1 else "each:"))
    for d in dict.fromkeys(_wrapper_dir(s) for s in sources):
        print(f"      {d}{os.sep}")
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
    """Patch a COPY into the "Patched Mods" wrapper. The .utoc/.ucas/.pak names
    are kept exactly (the loader keys off them); loader mod FOLDERS are renamed
    to match their .uplugin, which is what Dresscode keys off -- see
    _fix_loader_names. A folder is copied whole, a zip extracted, then patched in
    place with no backup (the untouched source is the backup); a lone .utoc goes
    through --out. Returns an exit code."""
    global _INTERACTED
    src = os.path.abspath(source.rstrip("\\/"))
    wrapper = _wrapper_dir(source)

    if _is_archive(src):
        dst = os.path.join(wrapper, os.path.splitext(os.path.basename(src))[0])
        ready = _UNPACKED.pop(os.path.normcase(src), None)
        if ready:
            # Already unpacked to see inside it; move that copy into place.
            print(f"  Extracting {os.path.basename(src)} to {dst} ...")
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            try:
                shutil.move(ready, dst)
            except Exception:
                shutil.copytree(ready, dst, dirs_exist_ok=True)
                shutil.rmtree(ready, ignore_errors=True)
        else:
            print(f"  Extracting {os.path.basename(src)} to {dst} ...")
            try:
                _extract_archive(src, dst)
            except Exception as ex:
                print(f"  Could not extract: {ex}")
                _INTERACTED = True
                return 0
            _expand_archives(dst)
        dst = _fix_loader_names(dst)
        return main(["--path", dst, "--all", "--no-backup"])

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
    _expand_archives(dst)
    dst = _fix_loader_names(dst)
    return main(["--path", dst, "--all", "--no-backup"])


def _confirm_game_folder(sources):
    """Drop was the game's own Mods/~mods (or inside them): a copy beside the
    original would just load twice, so confirm a straight in-place patch with
    central backups. Returns an exit code."""
    global _INTERACTED
    print("  ----------------------------------------------------------------")
    print("  That is inside your game install -- these are your installed mods.")
    print(f"  This {MODE['menu_verb']}es the ones that need it, in place. "
          "Your originals are")
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
        elif os.path.isdir(a) or a.lower().endswith((".utoc",) + _ARCHIVE_EXTS):
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
    return source is not None and any(_path_under(source, d)
                                      for d in _game_mod_dirs())


def _is_loader_root(source):
    """True when `source` IS the game's Mods (loader) folder -- the only place
    Dresscode lives, so the Dresscode note belongs only here. Never ~mods."""
    return (source is not None and _game_present()
            and _same_path(source, config.MODS_DIR))


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
    return os.path.join(root, MODE["local_backups"])


def main(argv):
    """Run the requested action. Returns a process exit code."""
    sources, out_dir, named, flags = _parse_args(argv)

    if any(f in flags for f in ("-h", "--help")):
        t = MODE["tool"]
        print(f"python {t} --list               list mods and what needs fixing")
        print(f"python {t} --all                {MODE['menu_verb']} everything "
              "that needs it (backups kept)")
        print(f"python {t} ModName              just one mod")
        print(f"python {t} --restore --all      undo everything")
        print(f'python {t} --path "D:\\mods"     work on a folder '
              "(add --all, --out, --restore)")
        print(f"Or just drag mod folders or archives onto {t}.")
        print("More: --debug adds detail to --list; --pause / --no-pause "
              "control the exit prompt.")
        return 0

    # Archives can't be scanned in place -- they are extracted and patched by
    # the drop menu -- so keep them out of find_mods but still count as work.
    archives = [s for s in sources if _is_archive(s)]
    # A dropped folder of archives has to be unpacked before its mods appear --
    # but never the installed library, where a leftover download is just
    # clutter and unpacking it would copy the whole library needlessly.
    packed = [s for s in sources
              if not _under_game_mods(s) and _contains_archive(s)]
    scan_sources = [s for s in sources if not _is_archive(s)]

    # Scan the library only with no sources at all -- an archive-only drop scans
    # nothing here (its mods appear once extracted), never the whole install.
    mods = find_mods(scan_sources) if scan_sources or not sources else {}
    if not mods and not archives and not packed:
        if sources:
            print("No mods (.utoc) found under:")
            for s in sources:
                print("   ", os.path.abspath(s))
        else:
            print("No mods found under", config.MODS_DIR)
            paks = getattr(config, "MODS_PAKS_DIR", "")
            if paks:
                print("                or", paks)
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
        needs_work = False
        if mods:
            needs_work = show_list(mods, debug, sources=scan_sources)
        # An archive is the one source that cannot be read where it lies, so
        # it used to be offered on faith. Look inside instead: one that needs
        # nothing is nothing to do, exactly as the same folder unpacked would
        # be. (The copy is kept, so saying yes does not unpack it twice.)
        # One already extracted and handled on an earlier run is reported as
        # the duplicate it is, which says more than "nothing to do" when the
        # unpacked copies are sitting right there -- so that is settled
        # first. Only when the scan itself is clean: with work pending the
        # menu processes the whole drop, archives included.
        every = archives + [arc for p in packed for arc in _archives_in(p)]
        dupe = set()
        if not needs_work:
            dupe = {os.path.normcase(os.path.abspath(a)) for a in every
                    if _archive_covered(a, mods)}
        settled = set()
        for n, a in enumerate(every, 1):
            if os.path.normcase(os.path.abspath(a)) in dupe:
                continue
            _peek_line("looking inside", n, len(every), a)
            if _archive_needs(a) is False:
                settled.add(os.path.normcase(os.path.abspath(a)))
        _peek_done()

        def _handled(a):
            return os.path.normcase(os.path.abspath(a)) in (settled | dupe)

        def _settled(a):
            return os.path.normcase(os.path.abspath(a)) in settled

        live_archives = [a for a in archives if not _handled(a)]
        live_packed = [p for p in packed
                       if not all(_handled(a) for a in _archives_in(p))]
        if live_archives or live_packed:
            print()
            print("  Archives to unpack and patch:")
            for a in live_archives + [p for p in live_packed
                                      if p not in live_archives]:
                print(f"    {os.path.abspath(a)}")
                if _is_archive(a):
                    _show_archive(a, " " * 8)
                else:
                    for arc in _archives_in(a):
                        print(f"        {os.path.basename(arc)}")
                        _show_archive(arc, " " * 12)
        skipped = ([a for a in archives if a not in live_archives]
                   + [arc for p in packed if p not in live_packed
                      for arc in _archives_in(p)])
        done = [a for a in skipped if _settled(a)]
        dupes = [a for a in skipped if not _settled(a)]
        if done:
            which = "this archive" if len(done) == 1 else "these archives"
            print()
            print(f"  Nothing to do in {which} -- what is inside is "
                  f"{MODE['done_inside']}:")
            for arc in done:
                print(f"    {os.path.basename(arc)}")
        if dupes:
            print()
            print("  Skipped -- the mods in these archives are already "
                  "listed above:")
            for arc in dupes:
                print(f"    {os.path.basename(arc)}")
        # A drop owns its window, so a bare listing would dead-end at the exit
        # pause -- offer the follow-up: in-place confirm for installed mods, the
        # copy flow for anything else (an archive is always the copy flow).
        # With nothing to do and nothing to unpack, there is nothing to offer.
        if sources and not (want_all or do_restore or named) and _owns_console():
            if not (live_archives or live_packed) and not needs_work:
                return 0
            if (scan_sources and not live_archives and not live_packed
                    and _all_under_game(scan_sources)):
                return _confirm_game_folder(scan_sources)
            menu_sources = sources
            if not needs_work:
                # The scanned folders are done; only the archives hold
                # anything unknown, so the offer covers just those.
                menu_sources = [s for s in sources
                                if s in live_archives or s in live_packed]
            return _folder_menu(menu_sources)
        return 0

    # Archives are handled by the drop menu, not this direct-action path.
    if (archives or packed) and not mods:
        tool = MODE["tool"]
        print(f"An archive is extracted and patched by dragging it onto {tool},")
        print(f"not with flags. Drop it on {tool}, or unpack it and use --path.")
        return 1

    targets = mods if want_all else {k: v for k, v in mods.items() if k in named}
    unknown = [n for n in named if n not in mods]
    if unknown:
        print(f"No mod called: {', '.join(unknown)} (check the spelling)")
    if not targets:
        print("Nothing selected. Use --list, --all, or name a mod.")
        print("Installed mods:" if not sources else "Mods found:",
              ", ".join(mods))
        return 1

    backup_base = _backup_root(scan_sources)
    print()
    changed, unchanged, failed = [], [], []
    for name, utoc in targets.items():
        print(name)
        if _is_dresscode(name) and not do_restore:
            print(f"    skipped -- {MODE['dresscode_skip'][0]}")
            for line in MODE["dresscode_skip"][1:]:
                print(f"    {line}")
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
                print(f"    note: still needs {r} installed -- "
                      "textures stay grey without it")
        except Exception as ex:
            print(f"    FAILED: {type(ex).__name__}: {ex}")
            failed.append(name)

    verb = "Restored" if do_restore else MODE["verb"]
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
                summary.append(f"  {MODE['verb']} copy is ready (your original"
                               " was left untouched):")
                summary.append(f"      {os.path.abspath(sources[0])}{os.sep}")
            else:
                summary.append(f"  {MODE['verb']} in place; no backup was taken"
                               " (--no-backup).")
        elif out_dir and not all(_same_path(out_dir, os.path.dirname(u))
                                 for u in targets.values()):
            summary.append(f"  {MODE['verb']} copies written to  "
                           f"{os.path.abspath(out_dir)}")
            summary.append("  Your original files were left exactly as they were.")
        else:
            summary.append("  Your untouched originals are backed up in:")
            summary.append(f"    {os.path.abspath(backup_base)}{os.sep}")
            summary.append("  Undo anytime with --restore.")

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


def run(argv):
    """Full command-line entry: startup checks, the run, the exit pause.
    Shared with unpatch.py, which flips MODE first."""
    # Folder mode (--path or dropped folders) needs only Oodle, not the game.
    _sources = _parse_args(argv)[0]
    code = 0 if startup(require_game=not _sources) else 1
    try:
        if code == 0:
            code = main(argv)
    finally:
        _discard_unpacked()     # whatever the scan opened and nothing claimed
    _pause_before_exit(argv)
    return code


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
