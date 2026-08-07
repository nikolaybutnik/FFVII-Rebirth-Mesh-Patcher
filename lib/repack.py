"""
repack.py -- writes a container back out with some of its chunks replaced.

Every tool that edits a mod ends the same way: most chunks are unchanged and
their compressed blocks can be copied across untouched, a handful were rewritten
and have to be re-compressed, and then the whole .utoc/.ucas pair is rebuilt
around them. That is this module, so there is one copy of it rather than one per
tool drifting out of step.

The caller supplies {chunk index: new bytes}. Anything the edit implies for the
CONTAINER HEADER (chunk type 10) -- export bundle sizes, say -- is the caller's
job: pass the rebuilt header in as just another replaced chunk. Edits that do
not change any package's length need nothing at all.
"""

import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import dirindex
import iostore
import writer

_POOL = None


def _pack_one(args):
    raw, comp_method = args
    comp = iostore.oodle_compress(raw) if comp_method else None
    ok = comp is not None and len(comp) < len(raw)
    if ok:
        try:
            ok = iostore.oodle_decompress(comp, len(raw)) == raw
        except Exception:
            ok = False
    return (comp, len(raw), comp_method) if ok else (raw, len(raw), 0)


def pack_blocks(payload, block_size, comp_method):
    """Split `payload` into <=block_size .ucas blocks, Oodle-compressing each so a
    rewritten mesh does not bloat the container (uncompressed it can double it).

    Every compressed block is verified to round-trip -- decompress back to the
    exact original -- before it is used. Anything that has no compressor, does
    not shrink, or does not round-trip is stored raw (method 0), so the output is
    always valid whatever the DLL does.

    Blocks compress on a thread pool -- the Oodle calls are stateless and
    ctypes releases the GIL, so this scales with cores. Order is preserved.
    """
    pieces = [(payload[k:k + block_size], comp_method)
              for k in range(0, len(payload), block_size)]
    if comp_method and len(pieces) > 8:
        global _POOL
        if _POOL is None:
            iostore.oodle_compress(b"\0")   # load the DLL once, serially
            _POOL = ThreadPoolExecutor(min(8, os.cpu_count() or 2))
        return list(_POOL.map(_pack_one, pieces, chunksize=8))
    return [_pack_one(p) for p in pieces]


def oodle_method(toc, say=None):
    """
    Oodle's index in THIS container's compression-method table.

    A wrong index would make the game misdecode our blocks, so it is read from
    the container rather than assumed. None means "store raw".
    """
    m = next((i for i, name in enumerate(toc.methods)
              if name.lower() == "oodle"), None)
    if m is None and len(toc.methods) > 1 and say:
        say(f"    note: container compresses with {', '.join(toc.methods[1:])}, "
            "not Oodle -- storing rewritten chunks uncompressed")
    return m


def write(toc, new_data, dst_dir, base, src_dir, copy_pak=True, say=print,
          progress=None, src_base=None):
    """
    Write toc's chunks to dst_dir/base.utoc/.ucas, replacing what new_data holds.

    `src_dir` is where the source .ucas lives -- untouched chunks are copied
    from it still compressed, which is the bulk of the speed. `src_base` is its
    stem, for the rare case where the output is named differently (the tools
    that keep a mod's name pass nothing). `copy_pak` brings the mod's .pak
    across, which the game needs to load the triple at all; skip it when
    writing over the original in place.

    Returns the number of bytes written to the .ucas.
    """
    src_base = src_base or base
    comp_method = oodle_method(toc, say)
    if progress is None:
        progress = sys.stdout.isatty()

    ucas_in = open(os.path.join(src_dir, src_base + ".ucas"), "rb")
    try:
        chunks = []
        new_paths = []
        for i in range(toc.n):
            if progress and i % 25 == 0:
                print(f"\r    reading chunk {i}/{toc.n}...", end="", flush=True)
            if i in new_data:
                payload = new_data[i]
                blocks = pack_blocks(payload, toc.block_size, comp_method)
                size = len(payload)
            else:
                # Untouched chunk: reuse its compressed blocks as-is.
                # build_metas_from reuses the source checksum row, so its
                # uncompressed bytes are never needed -- skipping this
                # decompress is the main speedup.
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
    finally:
        ucas_in.close()

    if progress:
        print("\r" + " " * 40 + "\r", end="", flush=True)

    directory = dirindex.build_dir_index(toc.mount, new_paths)
    body, ucas, _offlen, block_table = writer.build_container(
        toc, chunks, toc.block_size)
    head = writer.build_toc_header(toc, len(chunks), len(block_table),
                                   len(directory), toc.block_size)
    metas = writer.build_metas_from(toc, new_data)

    say(f"    writing {len(ucas) / (1024 * 1024):,.0f} MB to disk...")
    os.makedirs(dst_dir, exist_ok=True)
    with open(os.path.join(dst_dir, base + ".utoc"), "wb") as f:
        f.write(head + bytes(body) + directory + metas)
    with open(os.path.join(dst_dir, base + ".ucas"), "wb") as f:
        f.write(ucas)
    # The .pak is never rewritten, but the game loads a mod as a triple -- copy
    # it across whenever the original is not being overwritten in place.
    if copy_pak:
        pak_src = os.path.join(src_dir, src_base + ".pak")
        if os.path.exists(pak_src):
            shutil.copy(pak_src, os.path.join(dst_dir, base + ".pak"))
    return len(ucas)
