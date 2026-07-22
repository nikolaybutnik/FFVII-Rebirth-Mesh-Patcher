"""
writer.py -- rebuilds the .ucas data file and the middle sections of a .utoc.

This is the counterpart to iostore.py: that reads containers, this writes them.

HOW WE PROVED THIS IS CORRECT
-----------------------------
Writing a binary format from a reverse-engineered spec is exactly the kind of job
where you can be subtly wrong and not find out until the game crashes with no
useful error.

So before trusting it on a modified build, we ran it on the UNMODIFIED mod and
compared the result to the original byte for byte. It reproduces Dresscode's
original .utoc exactly -- 10821 bytes, all 51 chunks identical.

That single test catches endianness mistakes, alignment errors, and the
virtual-vs-physical offset confusion all at once. `verify.py --roundtrip` runs it.
Always do this after touching this file.

THE TWO ADDRESS SPACES
----------------------
The trickiest part of the format:

  VIRTUAL   What goes in the .utoc offset field. Each chunk starts on a fresh
            64KB boundary, so offsets step by 65536 no matter how tiny the chunk.

  PHYSICAL  Where bytes actually sit in the .ucas. Blocks are packed back to
            back, each starting at a 16-byte aligned position.

Mixing these up produces a container that looks valid and reads as garbage.
"""

import struct


def build_container(toc, chunks, block_size=65536):
    """
    Lay out chunks into a .ucas and build the matching .utoc middle sections.

    chunks: a list of dicts, one per chunk, each:
        {
          "id":     12 raw bytes (the chunk ID, copied from the source),
          "blocks": [(block_bytes, uncompressed_size, method), ...],
          "size":   total uncompressed length of the chunk,
        }

    For unchanged chunks we pass the ORIGINAL compressed blocks straight through,
    so we never need an Oodle compressor. For chunks we modified we pass a single
    uncompressed block with method 0. Method 0 is always legal, so this is safe;
    it just costs some file size (~90KB here).

    Returns (toc_body, ucas_bytes, offlen, block_table).
    `toc_body` is everything between the .utoc header and the directory index --
    the caller adds the header, directory index and checksums.
    """
    ucas = bytearray()
    block_table = []        # (physical_offset, compressed_size, usize, method)
    offlen = []             # (virtual_offset, length) per chunk
    block_index = 0

    for c in chunks:
        # Each chunk begins on a fresh block, so its virtual offset is simply
        # its starting block number times the block size.
        offlen.append((block_index * block_size, c["size"]))

        for raw, usize, method in c["blocks"]:
            # Pad to a 16-byte boundary BEFORE recording the position, so the
            # offset we store is where the data really starts.
            while len(ucas) % 16:
                ucas.append(0)
            block_table.append((len(ucas), len(raw), usize, method))
            ucas += raw
            block_index += 1

    body = bytearray()

    # --- Chunk IDs (12 bytes each) ---
    for c in chunks:
        body += bytes(c["id"])

    # --- Offsets and lengths (10 bytes each, BIG-endian -- the format's one
    #     inconsistency, and an easy thing to get wrong).
    for offset, length in offlen:
        body += offset.to_bytes(5, "big") + length.to_bytes(5, "big")

    # --- Compression blocks (12 bytes each, bit-packed little-endian) ---
    for phys_off, csize, usize, method in block_table:
        e = bytearray(12)
        e[0:5] = phys_off.to_bytes(5, "little")
        e[5:8] = csize.to_bytes(3, "little")
        e[8:11] = usize.to_bytes(3, "little")
        e[11] = method
        body += e

    # --- Compression method names, each padded to a fixed width. Index 0 is
    #     implicitly "None" and is not stored, so we start from methods[1].
    for i in range(toc.cm_count):
        name = toc.methods[i + 1].encode()
        body += name + b"\0" * (toc.cm_len - len(name))

    return body, bytes(ucas), offlen, block_table


def build_toc_header(toc, n_chunks, n_blocks, dir_index_size, block_size=65536):
    """
    Build the 144-byte .utoc header.

    Most fields are copied from the source container -- we're only ever changing
    how many chunks and blocks there are, never the container's identity or its
    compression settings.
    """
    h = bytearray(144)
    h[0:16] = b"-==--==--==--==-"        # magic
    h[16] = toc.version
    struct.pack_into(
        "<8I", h, 0x14,
        144,                # header size
        n_chunks,
        n_blocks,
        12,                 # bytes per compression block entry
        toc.cm_count,
        toc.cm_len,
        block_size,
        dir_index_size,
    )
    struct.pack_into("<Q", h, 0x38, toc.container_id)
    h[0x50] = toc.flags
    return bytes(h)


def build_metas_from(toc, modified):
    """
    Build the chunk checksum table, reusing the source container's rows.

    Each 33-byte row is SHA-1 of a chunk's uncompressed data + 12 zero bytes + a
    flags byte. An untouched chunk's row equals the one already in the source
    .utoc, so copy those and recompute only `modified` (index -> new payload).
    This avoids decompressing the whole container just to re-hash it -- the
    largest cost when patching texture-heavy mods.

    Valid because patch_mod's rebuild keeps chunk order and count identical to
    the source, so row i describes the same chunk in both.
    """
    import hashlib
    out = bytearray(toc.d[toc.meta_off: toc.meta_off + toc.n * 33])
    for i, payload in modified.items():
        out[i * 33: (i + 1) * 33] = (
            hashlib.sha1(payload).digest() + b"\x00" * 12 + bytes([1]))
    return bytes(out)
