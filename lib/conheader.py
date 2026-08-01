"""
conheader.py -- the container header chunk (chunk type 10).

Every container carries one, and it is the only place that knows which packages
the container HOLDS and which packages each of them DEPENDS ON -- both as raw
64-bit IDs. Rename a package and this goes stale even though every package
itself is perfectly correct.

LAYOUT
------
    0x00  container ID (u64)
    0x08  a preamble this module deliberately does not interpret (see below)
    0x20  package count (u32), then that many package IDs (u64 each)
          u32 byte size of the store-entry blob, then the blob:
            one 32-byte entry per package, in the same order as the IDs:
              +0   ExportBundlesSize (u64)
              +8   ExportCount (i32)
              +12  ExportBundleCount (i32)
              +16  LoadOrder (u32)
              +20  Pad (u32)
              +24  ImportedPackages: {u32 count, u32 offset}
          followed by the imported-package arrays the views point into.

The offset in an ImportedPackages view is relative to the VIEW's own position
(entry + 24), not to the blob -- reading it as blob-relative yields plausible
garbage.

ABOUT THE PREAMBLE
------------------
Bytes 0x08..0x20 hold the package count again plus two empty name arrays and the
name-hash algorithm ID. The exact field boundaries are not settled: a reading
that explains every byte has not been confirmed against a second container
shape. Since nothing here needs to CHANGE them, they are copied verbatim and
the package count is read from 0x20, which is the offset the patcher has used in
production. Do not "tidy" this into a full parse without checking it against
several containers first.
"""

import struct

COUNT_OFFSET = 0x20

# The package count is stored TWICE -- here and at COUNT_OFFSET -- and both must
# agree. Changing only one leaves a header that parses and then misbehaves.
COUNT_ECHO_OFFSET = 0x08

# Bytes 0x0c..0x20 hold two name arrays that are empty in every mod container
# seen. A non-empty one would shift everything after it, so rebuilding refuses
# rather than guess.
NAMES_OFFSET = 0x0C


def set_container_id(data, container_id):
    """
    Stamp the container's own ID into the header chunk.

    The ID is recorded TWICE: at 0x38 of the .utoc header, and in the first 8
    bytes of this chunk. Changing only the .utoc leaves the container registered
    under one ID and describing itself as another, which is fatal at startup and
    invisible to any check that reads the container's contents.
    """
    out = bytearray(data)
    struct.pack_into("<Q", out, 0, container_id)
    return bytes(out)


def parse(data):
    """
    Locate the parts of a header. Returns None if this is not a shape we can
    edit -- some packers emit a tiny header with no package list at all, and
    those must be passed through untouched rather than guessed at.
    """
    if len(data) < COUNT_OFFSET + 8:
        return None
    count = struct.unpack_from("<I", data, COUNT_OFFSET)[0]
    ids_off = COUNT_OFFSET + 4
    store_size_off = ids_off + count * 8
    if count == 0 or store_size_off + 4 > len(data):
        return None
    store_size = struct.unpack_from("<I", data, store_size_off)[0]
    store_off = store_size_off + 4
    if store_off + store_size > len(data) or store_size < count * 32:
        return None
    return dict(count=count, ids_off=ids_off,
                store_off=store_off, store_size=store_size)


def package_ids(data, info):
    return list(struct.unpack_from(f"<{info['count']}Q", data, info["ids_off"]))


def imported_packages(data, info, index):
    """The package IDs entry `index` depends on."""
    view = info["store_off"] + index * 32 + 24
    num, off = struct.unpack_from("<II", data, view)
    if not num:
        return []
    return list(struct.unpack_from(f"<{num}Q", data, view + off))


# ExportBundlesSize keeps flags in its top bits -- localized packages in the
# game's own containers set one. Only the low bits are the length.
SIZE_MASK = (1 << 62) - 1


def remap(data, pkgid_map, sizes=None):
    """
    Rewrite every package ID in the header through `pkgid_map`, and set each
    package's ExportBundlesSize from `sizes` ({old package ID -> new length}).

    THE SIZE IS NOT OPTIONAL. ExportBundlesSize is how many bytes the loader
    reads for a package, and it must equal the chunk's length exactly. Renaming
    a package resizes its name table and so its chunk, and a stale size here
    hands the loader a truncated package -- which crashes the game on startup,
    with nothing to say it came from this field.

    Everything is fixed width, so this edits in place and the header stays
    exactly as long as it was. Returns the data unchanged if the header is not
    an editable shape.
    """
    info = parse(data)
    if not info or (not pkgid_map and not sizes):
        return data

    out = bytearray(data)
    for i, pid in enumerate(package_ids(data, info)):
        if sizes and pid in sizes:
            o = info["store_off"] + i * 32
            old = struct.unpack_from("<Q", out, o)[0]
            struct.pack_into("<Q", out, o, (old & ~SIZE_MASK) | sizes[pid])
    for i in range(info["count"]):
        o = info["ids_off"] + i * 8
        old = struct.unpack_from("<Q", out, o)[0]
        if old in pkgid_map:
            struct.pack_into("<Q", out, o, pkgid_map[old])

    for i in range(info["count"]):
        view = info["store_off"] + i * 32 + 24
        num, off = struct.unpack_from("<II", out, view)
        for k in range(num):
            o = view + off + k * 8
            old = struct.unpack_from("<Q", out, o)[0]
            if old in pkgid_map:
                struct.pack_into("<Q", out, o, pkgid_map[old])

    return bytes(out)


def rebuild(data, keep, pkgid_map=None, sizes=None, keep_deps=False,
            extra_deps=None):
    """
    Emit a header listing only the packages in `keep` (a set of OLD package IDs),
    with IDs remapped through `pkgid_map` and lengths taken from `sizes`.

    Dropping a package is not just omitting its chunk: it has to leave the
    header too, and stop being named in any other package's imported-package
    list -- a dependency on a package that is no longer there is exactly the
    kind of dangling reference the loader follows straight into a null.

    `keep_deps` is for OVERLAY containers that always ride beside a pak
    serving the dropped packages: dependency entries stay (remapped through
    `pkgid_map` to where that pak serves them) even though the package
    itself leaves this container.

    `extra_deps` ({old package ID -> [NEW dependency IDs]}) appends to a
    package's list. A rewritten package that gained an import needs its new
    target listed here too -- the loader preloads imports from THIS list,
    and one it never heard of simply fails to resolve: grey checkers.

    Returns None if this header is not a shape we can rebuild.
    """
    info = parse(data)
    if not info:
        return None
    if struct.unpack_from("<I", data, NAMES_OFFSET)[0] != 0:
        return None                     # carries a name table; not our shape

    pkgid_map = pkgid_map or {}
    sizes = sizes or {}
    ids = package_ids(data, info)

    # Only the packages actually removed may leave a dependency list. Most IDs in
    # one belong to the GAME and are in no container of ours -- filtering to
    # "things we kept" silently strips every one, and a package whose
    # dependencies are not listed never gets them loaded. That renders as the
    # default checker material rather than as any kind of error.
    removed = set(ids) - set(keep)

    kept, entries, imports = [], [], []
    for i, pid in enumerate(ids):
        if pid not in keep:
            continue
        entry = bytearray(data[info["store_off"] + i * 32:
                               info["store_off"] + i * 32 + 32])
        if pid in sizes:
            old = struct.unpack_from("<Q", entry, 0)[0]
            struct.pack_into("<Q", entry, 0, (old & ~SIZE_MASK) | sizes[pid])
        kept.append(pkgid_map.get(pid, pid))
        entries.append(entry)
        deps = [pkgid_map.get(p, p)
                for p in imported_packages(data, info, i)
                if keep_deps or p not in removed]
        for extra in (extra_deps or {}).get(pid, ()):
            if extra not in deps:
                deps.append(extra)
        imports.append(deps)

    # Entries first, then every imported-package array, each addressed from its
    # own view field rather than from the start of the blob.
    store = bytearray(b"".join(bytes(e) for e in entries))
    for i, ids_for_entry in enumerate(imports):
        view = i * 32 + 24
        if not ids_for_entry:
            struct.pack_into("<II", store, view, 0, 0)
            continue
        struct.pack_into("<II", store, view, len(ids_for_entry), len(store) - view)
        store += struct.pack(f"<{len(ids_for_entry)}Q", *ids_for_entry)

    out = bytearray(data[:COUNT_OFFSET])
    struct.pack_into("<I", out, COUNT_ECHO_OFFSET, len(kept))
    out += struct.pack("<I", len(kept))
    out += struct.pack(f"<{len(kept)}Q", *kept) if kept else b""
    out += struct.pack("<I", len(store))
    out += store
    out += data[info["store_off"] + info["store_size"]:]

    # THE CHUNK MUST NOT SHRINK. Every real header chunk -- all of the game's
    # containers and every mod surveyed -- is an exact multiple of the 64KB
    # compression block size, and the game silently ignores one that is not:
    # the container still mounts and serves chunks, but its packages never
    # register, so the loader takes dependency lists from the game's own store
    # entry instead. Custom materials then never load and the model renders as
    # grey checkers, with nothing anywhere to say why. The dropped bytes were
    # zero padding; keep the original length.
    out += b"\0" * (len(data) - len(out))
    return bytes(out)
