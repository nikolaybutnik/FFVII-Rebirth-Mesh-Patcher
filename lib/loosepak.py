"""
loosepak.py -- write a ~mods container from a set of packages.

rename.py rewrites a container that already exists; this builds one from
package bytes alone, which is what any caller that ADDS or DUPLICATES a
package needs. The header is written from scratch rather than edited, so
the package list is whatever the caller passes.

Everything it produces is a loose pak: mounted at the content root, named
for the packages' own /Game/ paths, winning by overriding the game's copy.
"""

import hashlib
import os
import struct

import cityhash
import dirindex
import pakfile
import rename
import writer

# The container mount. Content root rather than a character folder, because
# what a mod overrides may reach anywhere under /Game/.
CONTENT_MOUNT = "../../../End/Content/"

# The name-hash algorithm ID every container carries in its header preamble.
NAME_HASH_ALGORITHM = 0xC1640000


def build_header(container_id, order, records):
    """
    The container header chunk: which packages this holds, how big each is,
    and what each depends on.

    Written rather than edited, so `conheader`'s "must not shrink" rule does
    not apply -- but the length one still does: the game ignores a header
    chunk that is not a whole number of compression blocks, mounting the
    container and registering none of its packages. That failure is silent
    and shows up as grey checkers, so the padding at the end is load-bearing.
    """
    out = struct.pack("<QIIIIQ", container_id, len(order), 0, 0, 8,
                      NAME_HASH_ALGORITHM)
    out += struct.pack("<I", len(order))
    out += b"".join(p.to_bytes(8, "little") for p in order)

    store = bytearray()
    for j, pid in enumerate(order):
        rec = records[pid]
        # ExportBundlesSize is how many bytes the loader reads for this
        # package and must equal the chunk exactly; stale by even 8 it
        # dereferences null on startup.
        size = len(rec["data"]) if "data" in rec else rec["size"]
        store += struct.pack("<QiiII", size, rec["exp"],
                             rec["bun"], j, 0xFFFFFFFF)
        store += struct.pack("<II", 0, 0)       # ImportedPackages view
    # The arrays the views point into, each addressed from its OWN view
    # field rather than from the start of the blob.
    for j, pid in enumerate(order):
        deps = records[pid]["deps"]
        if not deps:
            continue
        view = j * 32 + 24
        struct.pack_into("<II", store, view, len(deps), len(store) - view)
        store += struct.pack(f"<{len(deps)}Q", *deps)

    out += struct.pack("<I", len(store)) + store
    if len(out) % 65536:
        out += b"\0" * (65536 - len(out) % 65536)
    return out


def copied(toc, index, package_id=None, template=None):
    """
    Chunk `index` of `toc` as a spec to hand to `write`, taken exactly as it
    is stored: compressed blocks, length and checksum all copied, with only
    the package ID it is bound to restamped.

    Worth having because bulk data -- a mod's texture mips, most of its
    bytes -- holds no paths, so nothing here can ever need to edit it.
    Decompressing and re-Oodling it would be the slowest thing this tool
    does, to arrive at the same bytes.

    A block records its codec as an INDEX into its container's own name
    table, and the output's table comes from `template`. Where the two
    tables differ, the same index would mean a different codec, so the
    chunk is unpacked and recompressed instead. Pass `template` whenever the
    chunk is coming from some container other than the one being copied.
    """
    chunk_id = bytes(toc.chunk_ids[index])
    if package_id is not None:
        chunk_id = package_id.to_bytes(8, "little") + chunk_id[8:]
    if template is not None and list(template.methods) != list(toc.methods):
        return fresh(chunk_id, toc.read(index))
    return dict(id=chunk_id, blocks=toc.raw_blocks(index),
                size=toc.offlen[index][1], meta=toc.meta_row(index))


def fresh(chunk_id, payload):
    """A chunk spec for bytes this tool produced."""
    return dict(id=bytes(chunk_id), data=payload)


def write(order, records, out_dir, base, template, container_name=None):
    """
    Write out_dir/base.utoc + .ucas + .pak holding `order`'s packages.

        order       package IDs, in the order they go in the container
        records     {package ID: dict(name, data, exp, bun, deps, bulks)},
                    `bulks` being chunk specs from `copied` or `fresh`. A
                    record may carry `blocks`/`size`/`meta` of its own in
                    place of `data`, for a package that came back unchanged.
        template    an open Toc whose compression settings are copied

    `container_name` sets the container ID; without it the base name does.
    Two mods installed together must not share one.

    Returns the .utoc path.
    """
    cid = cityhash.package_id(container_name or base)
    header = build_header(cid, order, records)

    comp = next((m for m, n in enumerate(template.methods)
                 if n.lower() == "oodle"), None)

    chunks, metas, paths = [], [], []

    def add(spec):
        if "data" in spec:
            payload = spec["data"]
            chunks.append(dict(
                id=spec["id"], size=len(payload),
                blocks=rename.pack_blocks(payload, template.block_size, comp)))
            metas.append(hashlib.sha1(payload).digest() + b"\0" * 12 + b"\x01")
        else:
            chunks.append(dict(id=spec["id"], size=spec["size"],
                               blocks=spec["blocks"]))
            metas.append(spec["meta"])
        return len(chunks) - 1

    add(fresh(cid.to_bytes(8, "little") + b"\0\0\0\x0a", header))
    for pid in order:
        rec = records[pid]
        name = rec["name"]
        rel = name[len("/Game/"):] if name.lower().startswith("/game/") \
            else name.lstrip("/")
        spec = dict(rec, id=pid.to_bytes(8, "little") + b"\0\0\0\x02")
        paths.append((rel + ".uasset", add(spec)))
        for bulk in rec["bulks"]:
            ext = ".uptnl" if bulk["id"][11] == 4 else ".ubulk"
            paths.append((rel + ext, add(bulk)))

    directory = dirindex.build_dir_index(CONTENT_MOUNT, paths)
    body, ucas, _offlen, block_table = writer.build_container(
        template, chunks, template.block_size)
    head = bytearray(writer.build_toc_header(
        template, len(chunks), len(block_table), len(directory),
        template.block_size))
    struct.pack_into("<Q", head, 0x38, cid)
    metas = b"".join(metas)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, base + ".utoc"), "wb") as f:
        f.write(bytes(head) + bytes(body) + directory + metas)
    with open(os.path.join(out_dir, base + ".ucas"), "wb") as f:
        f.write(ucas)
    with open(os.path.join(out_dir, base + ".pak"), "wb") as f:
        f.write(pakfile.build(pakfile.LOOSE_MOUNT))
    return os.path.join(out_dir, base + ".utoc")
