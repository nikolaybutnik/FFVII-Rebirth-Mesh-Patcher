"""
pkgedit.py -- rewrites the header of a single package so it can be renamed.

zen.py reads a package; this edits one. Everything here exists to serve one
operation: change the PATHS a package knows about, without touching a byte of
the object data that follows.

WHAT A RENAME ACTUALLY TOUCHES
------------------------------
A package's name table holds its own path and the path of everything it refers
to. Those strings are the easy part. The hard part is that the same information
is ALSO stored three more times, as hashes:

  imports      one 64-bit ID per import, hashed from the target's object path
  exports      each export carries the ID other packages will import it BY
  graph data   the package IDs of every package this one depends on

Change a name and all three go stale. Nothing complains -- the package simply
resolves to nothing at load time.

THE ONE THING THAT DOES NOT MOVE
--------------------------------
Export data offsets. They are stored relative to the ORIGINAL uncooked file, and
the loader rebases them through CookedHeaderSize, which describes that original
file too. Growing or shrinking this header changes neither. Verified on 237
packages: export[0].off == cooked_hdr_size in every one.

LAYOUT NOTES THAT COST TIME TO REDISCOVER
-----------------------------------------
  * The name strings blob always starts at offset 64, right after the summary.
  * The name HASH blob is 8-byte aligned, zero padded, and is 8 * (count + 1)
    bytes -- the leading 8 being an algorithm ID, not a hash.
  * The import table starts immediately after the hash blob, no padding.
"""

import struct

import cityhash
from zen import ZenPackage

SUMMARY_SIZE = 64          # ZenPackage.HEADER, and where the name blob begins


def build_name_batch(names):
    """
    Serialize a name table back into its (strings, hashes) blobs.

    Headers and strings interleave -- header, string, header, string -- with the
    2-byte header packing a UTF-16 flag and a length. See load_name_batch for
    the read side; this is its exact inverse.
    """
    strings = bytearray()
    for s in names:
        wide = not s.isascii()
        raw = s.encode("utf-16-le") if wide else s.encode("ascii")
        length = len(raw) // 2 if wide else len(raw)
        if length > 0x7FFF:
            raise ValueError(f"name too long to encode: {s[:40]}...")
        strings += bytes([(0x80 if wide else 0) | (length >> 8), length & 0xFF])
        if wide and len(strings) & 1:
            strings.append(0)               # wide characters start 2-aligned
        strings += raw

    hashes = bytearray(struct.pack("<Q", cityhash.NAME_HASH_ALGORITHM))
    for s in names:
        hashes += struct.pack("<Q", cityhash.name_hash(s))

    return bytes(strings), bytes(hashes)


def export_object_path(pkg, export, renamed=None):
    """
    An export's path within its package: "Name", or "Outer/Name" nested.

    `renamed` overrides names by export index, so a path can be built from what
    the exports are ABOUT to be called rather than what they are now.

    Only NULL ends the chain. An outer of 0 is not "no outer" -- it is export
    number zero, the package's top-level object, and reading it as a terminator
    silently drops one level from the path (and so from the hash).
    """
    renamed = renamed or {}
    parts = [renamed.get(export["idx"], export["name"])]
    outer = export["outer"]
    seen = set()
    while outer != cityhash.NULL_INDEX and (outer >> 62) == 0:
        idx = outer & cityhash.M62
        if idx in seen or idx >= len(pkg.exports):
            break
        seen.add(idx)
        parent = pkg.exports[idx]
        parts.append(renamed.get(idx, parent["name"]))
        outer = parent["outer"]
    return "/".join(reversed(parts))


def split_name_number(name):
    """
    Encode a path the way the cook does: "/A/B_11" -> ("/A/B", 12).

    A package whose name carries an FName number stores only the BASE string
    in its name table, the number beside it -- name_at is the read side of
    this, split_name_number the write side. Only a trailing _<digits> with no
    leading zero splits: "_05" stays in the string, which is why costume slots
    _00.._09 never carry a number but PC0002_11 does.
    """
    stem, _, digits = name.rpartition("_")
    if not stem or not digits.isdigit() or len(digits) > 9 \
            or (digits != "0" and digits[0] == "0"):
        return name, 0
    return stem, int(digits) + 1


def package_name_of(pkg):
    return pkg.name_at(pkg.name & 0xFFFFFFFF, pkg.name >> 32)


def source_name_of(pkg):
    return pkg.name_at(pkg.srcname & 0xFFFFFFFF, pkg.srcname >> 32)


def rewrite(data, names=None, import_map=None, pkgid_map=None,
            new_package_name=None, new_source_name=None, export_names=None,
            fix_arcs=False, allow_shrink=False, extra_imports=(),
            extra_graph=()):
    """
    Rebuild one package's header.

        names             replacement name table, same length and order as the
                          original (indices stay valid, so nothing else moves)
        import_map        {old object ID -> new object ID} for the import table
        pkgid_map         {old package ID -> new package ID} for the graph data
        new_package_name  the package's own new path
        new_source_name   its new SOURCE path, which is what export IDs hash
                          from -- see below
        export_names      {export index -> new object name}, for when the object
                          inside has to be renamed too -- see below
        fix_arcs          rewrite graph arcs whose FromExportBundleIndex is -1
                          to 0 -- see below

    ABOUT fix_arcs: the Dresscode custom-engine cook emits dependency arcs with
    FromExportBundleIndex 0xFFFFFFFF. The game's own cook never does (0 of 300
    stock packages sampled), and the vanilla loader silently fails to load any
    package carrying one from a ~mods container -- probed in game: the packages
    that refuse to load are exactly the packages with -1 arcs. Working mods
    encode the same dependency as bundle index 0, so that is what we write.

    Returns the new package bytes. Object data is copied through untouched.

    Renaming a package is not enough to make it OVERRIDE a stock one. The game
    imports an object, not a package, and an import ID hashes the object's name
    along with its package's. So a mod mesh called MyOutfit, moved onto the stock
    costume's package, still answers to the wrong ID and resolves to nothing --
    which looks exactly like the mod not loading. export_names fixes that, and
    the name table is allowed to grow to hold the new name.

    A localized package (/Game/Sound/Voice/FR/...) carries a source name naming
    the original it was localized from (/Game/Sound/Voice/JP/...), and its
    exports are imported by IDs hashed from THAT, not from where it now lives.
    Hashing from the package name instead silently breaks every localized asset
    while leaving ordinary ones correct -- so it passes any test built only on
    mods, which have no localized packages at all.
    """
    pkg = ZenPackage(data)
    names = list(names) if names is not None else list(pkg.names)
    if len(names) < len(pkg.names) and not allow_shrink:
        # Indices into the table live in the object data too, where nothing
        # can rewrite them -- dropping a name from anywhere but an
        # unreferenced tail corrupts the package. allow_shrink is for the
        # round-trip restore, which drops exactly the tail name a forward
        # export rename appended.
        raise ValueError("the replacement name table may grow, but not shrink")

    # Renamed exports need their new name in the table. Appending keeps every
    # existing index valid, so nothing else has to be touched.
    renamed = {}
    for idx, new_name in (export_names or {}).items():
        if new_name not in names:
            names.append(new_name)
        renamed[idx] = (names.index(new_name), new_name)

    # -- graph data first, its size goes in the summary ------------------
    # A count, then that many (package ID, arc list). extra_graph appends
    # entries: a package that GAINED an import needs the exporting package
    # sequenced here, or the loader never brings it in -- an unlisted
    # import quietly resolves to nothing (grey checkers, not an error).
    graph = bytearray(data[pkg.graph_off:pkg.graph_off + pkg.graph_size])
    if (pkgid_map or fix_arcs) and len(graph) >= 4:
        count = struct.unpack_from("<I", graph, 0)[0]
        o = 4
        for _ in range(count):
            if o + 12 > len(graph):
                break
            pid = struct.unpack_from("<Q", graph, o)[0]
            if pkgid_map and pid in pkgid_map:
                struct.pack_into("<Q", graph, o, pkgid_map[pid])
            o += 8
            arcs = struct.unpack_from("<I", graph, o)[0]
            o += 4
            for _ in range(arcs):
                if fix_arcs and struct.unpack_from("<I", graph, o)[0] == 0xFFFFFFFF:
                    struct.pack_into("<I", graph, o, 0)
                o += 8
    if extra_graph:
        if len(graph) < 4:
            graph = bytearray(struct.pack("<I", 0))
        count = struct.unpack_from("<I", graph, 0)[0]
        struct.pack_into("<I", graph, 0, count + len(extra_graph))
        for pid, arcs in extra_graph:
            graph += struct.pack("<QI", pid, len(arcs))
            for arc_from, arc_to in arcs:
                graph += struct.pack("<ii", arc_from, arc_to)

    strings, hashes = build_name_batch(names)
    nm_off = SUMMARY_SIZE
    nh_off = (nm_off + len(strings) + 7) & ~7          # the blob is 8-aligned
    imp_off = nh_off + len(hashes)

    shift = imp_off - pkg.imp_off                      # everything after moves
    shift += 8 * len(extra_imports)                    # the table may grow
    exp_off = pkg.exp_off + shift
    bundles_off = pkg.bundles_off + shift
    graph_off = pkg.graph_off + shift

    # The summary name is (table index, number) and the number survives a
    # rename verbatim -- correct while the new leaf ends in the old suffix,
    # stale garbage the moment it does not (".../PC0002_11" carried number 12
    # onto "/Mod/Outfits/Foo" and the package answered as "Foo_11"). Re-encode
    # the number from the name the caller actually wants.
    def renumber(mapped, target):
        idx = mapped & 0x3FFFFFFF
        if target is None or idx >= len(names):
            return mapped
        base, number = split_name_number(target)
        if names[idx] != base:
            return mapped        # stored raw (or not this name) -- leave it
        return (mapped & 0xFFFFFFFF) | (number << 32)

    out = bytearray()
    out += struct.pack(
        ZenPackage.HEADER,
        renumber(pkg.name, new_package_name),
        renumber(pkg.srcname, new_source_name),
        pkg.pkg_flags, pkg.cooked_hdr_size,
        nm_off, len(strings), nh_off, len(hashes),
        imp_off, exp_off, bundles_off, graph_off, len(graph), pkg.pad)
    out += strings
    out += b"\0" * (nh_off - nm_off - len(strings))
    out += hashes

    # -- imports: values change; extra_imports appends, keeping existing
    #    positions (and so every FPackageIndex in the object data) valid --
    for imp in pkg.imports:
        out += struct.pack("<Q", (import_map or {}).get(imp, imp))
    for imp in extra_imports:
        out += struct.pack("<Q", imp)

    # -- exports: only the ID others import this one BY is recomputed -----
    owner = new_source_name or source_name_of(pkg)
    final_name = {i: n for i, (_slot, n) in renamed.items()}
    for e in pkg.exports:
        entry = bytearray(data[pkg.exp_off + e["idx"] * 72:
                               pkg.exp_off + e["idx"] * 72 + 72])
        if e["idx"] in renamed:
            slot, _n = renamed[e["idx"]]
            struct.pack_into("<II", entry, 16, slot, 0)
        if e["gimp"] != cityhash.NULL_INDEX:
            path = export_object_path(pkg, e, final_name)
            struct.pack_into("<Q", entry, 56, cityhash.object_id(owner, path))
        out += entry

    out += data[pkg.bundles_off:pkg.graph_off]
    out += graph
    out += data[pkg.export_data_start():]
    return bytes(out)
