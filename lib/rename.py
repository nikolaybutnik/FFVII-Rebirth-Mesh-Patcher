"""
rename.py -- give every package in a container a new path.

This is the engine underneath format conversion. A Dresscode costume and a loose
pak mod hold the same assets; what differs is where those assets CLAIM to live.
Dresscode's sit at /<ModName>/..., a loose pak's at /Game/..., and the game
mounts each accordingly. Converting between the two is, at bottom, this module.

WHAT HAS TO MOVE TOGETHER
-------------------------
A package path is recorded in eight places, and all eight must agree:

  1. the package's own name table                 pkgedit
  2. the name tables of every package quoting it  pkgedit
  3. its FPackageId in the .utoc chunk ID         here
  4. the same ID on its .ubulk / .uptnl chunks    here
  5. the container header's package list          conheader
  6. the imported-package list of every entry     conheader
  7. the import table of every referring package  pkgedit
  8. the export IDs it is imported BY             pkgedit

Miss one and nothing errors. The mod simply does not appear.

PROVING IT
----------
Renaming to the SAME names must reproduce the source container byte for byte --
the same test writer.py uses. Anything that fails that test is wrong in a way no
amount of reading the output will reveal.
"""

import os
import struct

import cityhash
import conheader
import dirindex
import iostore
import pkgedit
import writer
from zen import ZenPackage

PACKAGE_CHUNK = 2
HEADER_CHUNK = 10
BULK_CHUNKS = (3, 4)


def map_path(name, renames, exact=None):
    """
    Repoint one name-table string.

    Handles both a bare package path and an object path -- "/Mod/A/B" and
    "/Mod/A/B.B" alike -- by splitting at the first dot AFTER the last slash.
    Splitting at the last dot instead breaks subobject paths, and splitting at
    the first dot breaks any folder containing one.

    `exact` is consulted first, for object paths whose OBJECT half also changes;
    a prefix rewrite cannot express that.
    """
    if not name.startswith("/"):
        return name
    if exact and name.lower() in exact:
        return exact[name.lower()]
    slash = name.rfind("/")
    dot = name.find(".", slash + 1)
    package = name[:dot] if dot >= 0 else name
    new = renames.get(package.lower())
    if new is None:
        return name
    return new + (name[dot:] if dot >= 0 else "")


def read_packages(toc):
    """
    Survey a container: {package ID -> {name, exports, chunk}} for every package.

    Reading each package is the slow part of a conversion; everything downstream
    works from this one pass.
    """
    found = {}
    for i in range(toc.n):
        if toc.chunk_ids[i][11] != PACKAGE_CHUNK:
            continue
        pkg = ZenPackage(toc.read(i))
        pid = int.from_bytes(toc.chunk_ids[i][:8], "little")
        found[pid] = dict(
            chunk=i,
            name=pkgedit.package_name_of(pkg),
            exports=[pkgedit.export_object_path(pkg, e) for e in pkg.exports],
        )
    return found


def build_maps(packages, renames, object_renames=None, extra_imports=None):
    """
    Turn a name mapping into the ID mappings the rewrite needs.

    Returns (pkgid_map, import_map, string_map). Only renamed packages
    contribute, so anything pointing at the game itself is left exactly as it
    was. object_renames is {lowercased package name: {old object: new object}}.

    `extra_imports` supplies object-ID mappings for packages that are NOT in
    this container -- an overlay pak names objects its base pak serves, and
    only the caller knows what those are called on the other side.
    """
    object_renames = object_renames or {}
    pkgid_map, import_map, string_map = {}, {}, {}
    # Package IDs can be mapped from the names alone -- graph data and
    # header dependencies may reference packages this container dropped.
    for old_low, new_name in renames.items():
        old_pid, new_pid = (cityhash.package_id(old_low),
                            cityhash.package_id(new_name))
        if old_pid != new_pid:
            pkgid_map[old_pid] = new_pid
    if extra_imports:
        import_map.update(extra_imports)
    for pid, info in packages.items():
        old_name = info["name"]
        new_name = renames.get(old_name.lower())
        objects = object_renames.get(old_name.lower(), {})
        if new_name is None or (new_name == old_name and not objects):
            continue
        if new_name != old_name:
            pkgid_map[pid] = cityhash.package_id(new_name)
        for path in info["exports"]:
            head, _, rest = path.partition("/")
            new_path = objects.get(head, head) + (("/" + rest) if rest else "")
            old = cityhash.object_id(old_name, path)
            new = cityhash.object_id(new_name, new_path)
            if old != new:
                import_map[old] = new
            if new_path != path:
                string_map[f"{old_name}.{path}".lower()] = f"{new_name}.{new_path}"
    return pkgid_map, import_map, string_map


def _chunk_id_with(chunk_id, package_id):
    return package_id.to_bytes(8, "little") + bytes(chunk_id[8:])


def rewrite_chunks(toc, packages, renames, object_renames=None, dropped=None,
                   progress=None, fix_arcs=False, keep_deps=False,
                   extra_imports=None, post_edit=None, extra_deps=None):
    """
    Produce {chunk index -> new bytes} and {chunk index -> new 12-byte ID}.

    Every package goes through the rewrite, renamed or not -- but one that comes
    back unchanged is dropped from the result, so the caller reuses its original
    compressed blocks. That keeps an identity rename byte-identical (recompressing
    would not be) and avoids re-Oodling megabytes of untouched mesh.
    """
    object_renames = object_renames or {}
    dropped = dropped or set()
    pkgid_map, import_map, string_map = build_maps(packages, renames,
                                                   object_renames,
                                                   extra_imports)
    new_data, new_ids = {}, {}

    for pid, info in packages.items():
        if info["name"].lower() in dropped:
            continue
        i = info["chunk"]
        if progress:
            progress(i)
        data = toc.read(i)
        pkg = ZenPackage(data)
        new_name = renames.get(info["name"].lower(), info["name"])
        names = [map_path(n, renames, string_map) for n in pkg.names]
        objects = object_renames.get(info["name"].lower(), {})
        export_names = {e["idx"]: objects[e["name"]]
                        for e in pkg.exports if e["name"] in objects}
        # A package with an FName number stores its own path unsuffixed, so the
        # prefix rewrite above never matched it. Set that one entry directly.
        for mapped in (pkg.name, pkg.srcname):
            idx, number = mapped & 0x3FFFFFFF, mapped >> 32
            if number and idx < len(names):
                resolved = pkg.name_at(mapped & 0xFFFFFFFF, number)
                moved = renames.get(resolved.lower())
                if moved:
                    names[idx] = pkgedit.strip_name_number(moved, number)
        source = pkgedit.source_name_of(pkg)
        out = pkgedit.rewrite(
            data, names=names,
            import_map=import_map, pkgid_map=pkgid_map,
            new_package_name=new_name,
            new_source_name=renames.get(source.lower(), source),
            export_names=export_names, fix_arcs=fix_arcs)
        edit = (post_edit or {}).get(info["name"].lower())
        if edit:
            out = edit(out)
        if out != data:
            new_data[i] = out

    # Bulk chunks carry no paths of their own -- only the package ID that binds
    # them to their .uasset, which has just changed.
    for i in range(toc.n):
        pid = int.from_bytes(toc.chunk_ids[i][:8], "little")
        if toc.chunk_ids[i][11] == HEADER_CHUNK:
            continue
        if pid in pkgid_map:
            new_ids[i] = _chunk_id_with(toc.chunk_ids[i], pkgid_map[pid])

    # Rewriting a package resizes it, and the header records that size.
    sizes = {pid: len(new_data[info["chunk"]])
             for pid, info in packages.items() if info["chunk"] in new_data}

    keep = {pid for pid, info in packages.items()
            if info["name"].lower() not in (dropped or set())}

    for i in range(toc.n):
        if toc.chunk_ids[i][11] != HEADER_CHUNK:
            continue
        # Some packers write a tiny header labelled Oodle but stored raw, which
        # cannot be decoded at all. It lists no packages, so there is nothing in
        # it to remap -- pass it through rather than failing the conversion.
        try:
            header = toc.read(i)
        except Exception:
            continue
        if len(keep) != len(packages):
            out = conheader.rebuild(header, keep, pkgid_map, sizes,
                                    keep_deps=keep_deps,
                                    extra_deps=extra_deps)
            if out is None:
                raise RuntimeError("this container's header cannot be rebuilt, "
                                   "so packages cannot be dropped from it")
        else:
            out = conheader.remap(header, pkgid_map, sizes)
        if out != header:
            new_data[i] = out

    return new_data, new_ids


def rename_container(toc, renames, mount, path_for, out_dir, base,
                     container_name=None, object_renames=None, drop=None,
                     quiet=False, fix_arcs=False, cross_pak=False,
                     post_edit=None, extra_deps=None):
    """
    Write a renamed copy of `toc` as out_dir/base.utoc + .ucas.

        renames         {lowercased old package name -> new package name}
        mount           mount point for the output container
        path_for        new package name -> path inside the container, without
                        an extension
        container_name  gives the output a fresh container ID; without it the
                        source's ID is kept, which two installed mods must not
                        share
        cross_pak       the output is an OVERLAY that always rides beside
                        another pak serving the packages dropped here, so a
                        kept package importing a dropped one is by design,
                        not a dangling reference

    The .pak is NOT written here -- it belongs to the target format, not to the
    rename, and the two conversions need different ones.
    """
    packages = read_packages(toc)
    say = (lambda *a: None) if quiet else print

    dropped = {n.lower() for n in (drop or ())}
    gone = {pid for pid, info in packages.items()
            if info["name"].lower() in dropped}
    if gone:
        if not cross_pak:
            _refuse_if_needed(toc, packages, gone)
        say(f"    dropping {len(gone)} package(s) this form does not use")
    say(f"    rewriting {len(packages) - len(gone)} packages")
    new_data, new_ids = rewrite_chunks(toc, packages, renames, object_renames,
                                       dropped, fix_arcs=fix_arcs,
                                       keep_deps=cross_pak,
                                       post_edit=post_edit,
                                       extra_deps=extra_deps)

    # The container ID is recorded THREE times and all copies must agree: at
    # 0x38 of the .utoc header, in the first 8 bytes of the header chunk's
    # data, and in the first 8 bytes of the header chunk's CHUNK ID -- the
    # loader finds the header chunk by looking up (container id, type 10), so
    # a stale chunk ID means the header is never read: the container still
    # mounts and serves package chunks, but registers no package store
    # entries, and every package the mod ADDS silently fails to load.
    if container_name:
        cid = cityhash.package_id(container_name)
        for i in range(toc.n):
            if toc.chunk_ids[i][11] != HEADER_CHUNK:
                continue
            try:
                header = new_data.get(i) or toc.read(i)
            except Exception:
                break
            new_data[i] = conheader.set_container_id(header, cid)
            new_ids[i] = cid.to_bytes(8, "little") + bytes(toc.chunk_ids[i][8:])

    comp_method = next((m for m, name in enumerate(toc.methods)
                        if name.lower() == "oodle"), None)

    name_of_pid = {pid: renames.get(info["name"].lower(), info["name"])
                   for pid, info in packages.items()}

    ucas_in = open(os.path.splitext(toc.path)[0] + ".ucas", "rb")
    skip = {info["chunk"] for pid, info in packages.items() if pid in gone}
    skip |= {i for i in range(toc.n)
             if int.from_bytes(toc.chunk_ids[i][:8], "little") in gone}
    chunks, paths = [], []
    for i in range(toc.n):
        if i in skip:
            continue
        if i in new_data:
            payload = new_data[i]
            blocks = pack_blocks(payload, toc.block_size, comp_method)
            size = len(payload)
        else:
            offset, length = toc.offlen[i]
            b, remaining, blocks = offset // toc.block_size, length, []
            while remaining > 0:
                pos, csize, usize, method = toc.blocks[b]
                ucas_in.seek(pos)
                blocks.append((ucas_in.read(csize), usize, method))
                remaining -= usize
                b += 1
            size = length
        chunks.append(dict(id=new_ids.get(i, toc.chunk_ids[i]),
                           blocks=blocks, size=size))
        if i in toc.paths:
            pid = int.from_bytes(toc.chunk_ids[i][:8], "little")
            ext = os.path.splitext(toc.paths[i])[1]
            new_name = name_of_pid.get(pid)
            paths.append((path_for(new_name) + ext if new_name else toc.paths[i],
                          len(chunks) - 1))
    ucas_in.close()

    directory = dirindex.build_dir_index(mount, paths)
    body, ucas, _offlen, block_table = writer.build_container(
        toc, chunks, toc.block_size)
    head = bytearray(writer.build_toc_header(
        toc, len(chunks), len(block_table), len(directory), toc.block_size))
    if container_name:
        struct.pack_into("<Q", head, 0x38, cityhash.package_id(container_name))
    metas = writer.build_metas_from(toc, new_data)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, base + ".utoc"), "wb") as f:
        f.write(bytes(head) + bytes(body) + directory + metas)
    with open(os.path.join(out_dir, base + ".ucas"), "wb") as f:
        f.write(ucas)
    say(f"    written {base}.utoc/.ucas ({len(ucas) / (1024 * 1024):,.1f} MB)")

    written = os.path.join(out_dir, base + ".utoc")
    lost = 0 if cross_pak else \
        _dependencies_lost(toc, iostore.Toc(written), gone)
    if lost:
        raise RuntimeError(
            f"the rebuilt header lost {lost} package dependencies -- the mod "
            "would load with unresolved materials")
    return written


def _dependency_total(toc, skip):
    """
    How many package dependencies the header records.

    `skip` names packages being removed, so both their own lists and every
    reference TO them are left out -- what remains is what a correct rebuild
    must still carry.
    """
    for i in range(toc.n):
        if toc.chunk_ids[i][11] != HEADER_CHUNK:
            continue
        try:
            raw = toc.read(i)
        except Exception:
            return None
        info = conheader.parse(raw)
        if not info:
            return None
        ids = conheader.package_ids(raw, info)
        return sum(len([p for p in conheader.imported_packages(raw, info, k)
                        if p not in skip])
                   for k in range(info["count"]) if ids[k] not in skip)
    return None


def _dependencies_lost(source, result, gone):
    """
    Dependencies dropped between source and result, beyond the intended ones.

    Every entry in a package's dependency list has to survive a rename -- most
    of them name GAME packages, and losing those is invisible to every
    structural check while leaving the mod's materials unresolved in game.
    """
    before = _dependency_total(source, gone)
    after = _dependency_total(result, set())
    if before is None or after is None:
        return 0
    return max(0, before - after)


def _refuse_if_needed(toc, packages, gone):
    """
    Refuse to drop a package anything still points at.

    Dropping is only safe for a leaf. If a surviving package imports one of the
    doomed exports, removing it leaves an import that resolves to nothing --
    trading one broken mod for another, less obvious one.
    """
    doomed = set()
    for pid in gone:
        info = packages[pid]
        for path in info["exports"]:
            doomed.add(cityhash.object_id(info["name"], path))

    for pid, info in packages.items():
        if pid in gone:
            continue
        pkg = ZenPackage(toc.read(info["chunk"]))
        if any(imp in doomed for imp in pkg.imports):
            raise RuntimeError(
                f"cannot drop the Dresscode data assets: {info['name']} "
                "still refers to them")


def verify(utoc_path):
    """
    Re-open a written container and check it against itself. Returns a list of
    problems, empty if sound.

    Worth doing on every conversion, because all three of these are invisible
    until the game loads the mod, and two of them crash it outright:

      * ExportBundlesSize must equal the chunk's length. This is the byte count
        the loader reads for a package; stale by even 8 bytes it hands itself a
        truncated package and dereferences null on startup.
      * a package's ID must be the hash of its own name, or the loader never
        finds it.
      * an export's ID must be the hash of its path, or nothing that imports it
        resolves -- which looks exactly like the mod not being installed.
    """
    toc = iostore.Toc(utoc_path)
    problems = []

    entries = {}
    for i in range(toc.n):
        if toc.chunk_ids[i][11] != HEADER_CHUNK:
            continue
        try:
            raw = toc.read(i)
        except Exception:
            break                       # unreadable header, nothing to check
        stamped = struct.unpack_from("<Q", raw, 0)[0]
        if stamped != toc.container_id:
            problems.append(
                f"container ID mismatch: .utoc says {toc.container_id:#018x}, "
                f"the header chunk says {stamped:#018x}")
        # Third copy: the header chunk's own CHUNK ID is (container id, type
        # 10), and the loader finds the header BY that ID. Stale here means no
        # package store entries at all -- overrides still work, added packages
        # silently don't, and the model renders with checkered materials.
        chunk_cid = int.from_bytes(toc.chunk_ids[i][:8], "little")
        if chunk_cid != toc.container_id:
            problems.append(
                f"container ID mismatch: .utoc says {toc.container_id:#018x}, "
                f"the header chunk's chunk ID says {chunk_cid:#018x}")
        info = conheader.parse(raw)
        if not info:
            break
        for k, pid in enumerate(conheader.package_ids(raw, info)):
            size = struct.unpack_from("<Q", raw, info["store_off"] + k * 32)[0]
            entries[pid] = size & conheader.SIZE_MASK

    for i in range(toc.n):
        if toc.chunk_ids[i][11] != PACKAGE_CHUNK:
            continue
        pid = int.from_bytes(toc.chunk_ids[i][:8], "little")
        path = toc.paths.get(i, f"chunk {i}")
        pkg = ZenPackage(toc.read(i))
        name = pkgedit.package_name_of(pkg)

        if cityhash.package_id(name) != pid:
            problems.append(f"{path}: package ID is not the hash of {name}")
        if entries and pid not in entries:
            problems.append(f"{path}: missing from the container header")
        elif entries and entries[pid] != toc.offlen[i][1]:
            problems.append(f"{path}: header says {entries[pid]} bytes, "
                            f"chunk is {toc.offlen[i][1]}")

        source = pkgedit.source_name_of(pkg)
        for e in pkg.exports:
            if e["gimp"] == cityhash.NULL_INDEX:
                continue
            want = cityhash.object_id(source, pkgedit.export_object_path(pkg, e))
            if want != e["gimp"]:
                problems.append(f"{path}: export {e['name']} has the wrong ID")
    return problems


def pack_blocks(payload, block_size, comp_method):
    """Split into blocks, Oodle-compressing each only where it round trips."""
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
