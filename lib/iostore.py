"""
iostore.py -- reads Unreal Engine "IoStore" containers (.utoc / .ucas).

WHAT THIS IS
------------
A mod ships as three files:

    DresscodeEnd-WindowsNoEditor.utoc   the index  ("what's inside, and where")
    DresscodeEnd-WindowsNoEditor.ucas   the data   (everything, compressed)
    DresscodeEnd-WindowsNoEditor.pak    a stub Unreal still expects to exist

Compare it to a ZIP file: the .ucas is the compressed bytes and the .utoc is the
table of contents. This module reads the .utoc, then uses it to pull individual
items ("chunks") out of the .ucas.

A chunk is usually one package -- one .uasset file, i.e. one game asset.

THE ONE BIG GOTCHA
------------------
A chunk's stored "offset" is NOT a position in the .ucas file. It is a position in
an imaginary uncompressed layout. To find the real data you convert it to a block
number:

    block_index = offset / compression_block_size

and then look that block up in the block table, which holds the true .ucas position.

This trips up everyone (it certainly tripped up me).

"""

import ctypes
import os
import struct

import config

# ---------------------------------------------------------------------------
# Oodle (de)compression
# ---------------------------------------------------------------------------
# The container's data is compressed with Oodle, a proprietary library. There is
# no Python implementation, so we call the real DLL through ctypes (Python's
# "call a C function" mechanism).
#
# Unchanged chunks reuse their original compressed bytes untouched, so mostly we
# only DEcompress. We also compress the chunks we rewrite (oodle_compress, below)
# so a patched mesh does not double the container -- and we do NOT need to match
# the build's original encoder settings, because the game reads any Oodle
# codec/level from each block's own header (proven across oo2core 6/7/9).
# ---------------------------------------------------------------------------

# The DLL is loaded ON FIRST USE, not at import time. That distinction matters:
# loading it at import means a machine without one cannot even import this
# module, so the tool dies with a raw ctypes OSError before it can explain the
# problem or offer to help. Deferring the load keeps that failure recoverable.
_oodle = None


def _load_oodle():
    global _oodle
    if _oodle is not None:
        return _oodle

    path = getattr(config, "OODLE_DLL", "")
    if not path:
        raise RuntimeError("No Oodle library configured.")
    if not os.path.exists(path):
        raise RuntimeError(f"Oodle library not found: {path}")

    try:
        lib = ctypes.CDLL(path)
    except OSError as ex:
        raise RuntimeError(f"Could not load {path}: {ex}") from None
    if not hasattr(lib, "OodleLZ_Decompress"):
        raise RuntimeError(
            f"{os.path.basename(path)} has no OodleLZ_Decompress -- "
            "it does not look like an Oodle core library.")

    lib.OodleLZ_Decompress.restype = ctypes.c_int64
    lib.OodleLZ_Decompress.argtypes = [
        ctypes.c_char_p, ctypes.c_int64,     # compressed input + its length
        ctypes.c_char_p, ctypes.c_int64,     # output buffer + expected length
        ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int64, ctypes.c_int,
    ]
    _oodle = lib
    return _oodle


def oodle_decompress(src, out_size):
    """Decompress `src` into exactly `out_size` bytes."""
    _oodle = _load_oodle()
    out = ctypes.create_string_buffer(out_size)
    # The trailing arguments are fuzz-safety and threading options we don't use;
    # these are the values Unreal itself passes.
    n = _oodle.OodleLZ_Decompress(src, len(src), out, out_size,
                                  1, 1, 0, None, 0, None, None, None, 0, 3)
    if n <= 0:
        # Oodle refused the data outright. The usual cause is a DLL too old to
        # decode this game's compression -- oo2core_5 and older do not work with
        # FFVII Rebirth; use oo2core_6 or newer.
        raise RuntimeError(
            f"Oodle could not decode this data (returned {n}). The oo2core DLL is "
            "probably too old or incompatible -- use oo2core_6 or newer."
        )
    if n != out_size:
        raise RuntimeError(
            f"Oodle returned {n} bytes but {out_size} were expected. "
            "This almost always means the block table was parsed incorrectly."
        )
    return out.raw[:out_size]


# Kraken, at a middling level. The game reads any Oodle codec/level from each
# block's own header, so we need NOT match the build's original encoder settings
# -- a valid Oodle block is a valid Oodle block. The patcher round-trips every
# block it compresses (decompress == original) before trusting it.
_OODLE_KRAKEN = 8
_OODLE_LEVEL = 4


def oodle_compress(src):
    """Kraken-compress `src`, or None if the DLL exposes no compressor or the
    call failed. The caller decides whether the result actually helped and
    verifies it round-trips."""
    lib = _load_oodle()
    if not hasattr(lib, "OodleLZ_Compress"):
        return None
    fn = lib.OodleLZ_Compress
    fn.restype = ctypes.c_int64
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int64, ctypes.c_char_p,
                   ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_int64]
    out = ctypes.create_string_buffer(len(src) + len(src) // 16 + 512)
    n = fn(_OODLE_KRAKEN, src, len(src), out, _OODLE_LEVEL,
           None, None, None, None, 0)
    return out.raw[:n] if n > 0 else None


# Marks a "no entry here" link in the directory tree.
INVALID = 0xFFFFFFFF


class Toc:
    """
    A parsed IoStore container, opened read-only.

        toc  = Toc("path/to/Mod.utoc")    # the .ucas is found automatically
        data = toc.read(4)                # decompressed bytes of chunk 4
        print(toc.paths[4])               # "Assets/Empty/Empty.uasset"

    Useful attributes:
        n            number of chunks
        chunk_ids    12-byte ID per chunk (last byte = type; 2=package, 10=header)
        offlen       (virtual_offset, uncompressed_length) per chunk
        blocks       (ucas_offset, compressed_size, uncompressed_size, method)
        paths        {chunk_index: "path/inside/mod.uasset"}
        mount        path prefix the game mounts this container at
    """

    def __init__(self, utoc_path):
        data = open(utoc_path, "rb").read()
        self.d = data

        if data[:16] != b"-==--==--==--==-":
            raise ValueError("Not an IoStore .utoc file (bad magic bytes)")

        self.version = data[16]

        # --- Header. Every size we need is in here. ---
        (self.hdr_size, self.n, self.cb_count, self.cb_size,
         self.cm_count, self.cm_len, self.block_size,
         self.dir_size) = struct.unpack_from("<8I", data, 0x14)

        self.container_id = struct.unpack_from("<Q", data, 0x38)[0]
        # Flags: 1=Compressed, 2=Encrypted, 4=Signed, 8=Indexed.
        # Dresscode is 0x09 -> compressed + indexed, NOT encrypted or signed.
        # That is precisely why this whole fix is possible.
        self.flags = data[0x50]

        o = self.hdr_size

        # --- Chunk IDs: 12 bytes each. Byte 11 is the chunk type. ---
        self.chunk_ids = [data[o + i * 12: o + i * 12 + 12] for i in range(self.n)]
        o += self.n * 12

        # --- Offsets and lengths: 10 bytes each, and BIG-endian (unlike the rest
        #     of the file). Remember the offset is virtual, not a file position.
        self.offlen = []
        for i in range(self.n):
            raw = data[o + i * 10: o + i * 10 + 10]
            self.offlen.append((int.from_bytes(raw[0:5], "big"),
                                int.from_bytes(raw[5:10], "big")))
        o += self.n * 10

        # --- Compression blocks: 12 bytes each, bit-packed. This table is what
        #     maps a block number to its real position inside the .ucas.
        self.cb_off = o
        self.blocks = []
        for _ in range(self.cb_count):
            raw = data[o: o + 12]
            self.blocks.append((
                int.from_bytes(raw[0:5], "little"),    # position in .ucas
                int.from_bytes(raw[5:8], "little"),    # compressed size
                int.from_bytes(raw[8:11], "little"),   # uncompressed size
                raw[11],                               # 0 = stored, 1+ = a method
            ))
            o += 12

        # --- Compression method names. Index 0 is implicitly "None", so the
        #     stored names begin at index 1.
        self.methods = ["None"]
        for _ in range(self.cm_count):
            self.methods.append(data[o:o + self.cm_len].split(b"\0")[0].decode())
            o += self.cm_len

        # --- Directory index: the filenames. Only present when the Indexed flag
        #     is set -- the game's global.utoc has none, hence the guard below.
        self.dir_off = o
        self.dir_raw = data[o:o + self.dir_size]
        o += self.dir_size

        # --- Chunk metas: 33 bytes each (SHA-1 + 12 zero bytes + a flags byte).
        self.meta_off = o

        self.ucas = open(utoc_path.replace(".utoc", ".ucas"), "rb")
        self.paths = self.parse_directory_index(self.dir_raw) if self.dir_size else {}

    # -- filenames -----------------------------------------------------------

    def parse_directory_index(self, blob):
        """
        Rebuild {chunk_index: path} by walking the stored folder tree.

        The tree uses linked lists rather than nested arrays: each folder points
        to its first child and its next sibling; each file points to the next
        file in the same folder. INVALID (0xFFFFFFFF) terminates a list.
        """
        b = blob
        o = 0

        def read_string(o):
            n = struct.unpack_from("<i", b, o)[0]
            o += 4
            if n == 0:
                return "", o
            if n < 0:      # a negative length would mean UTF-16 (not used here)
                return b[o:o - n * 2].decode("utf-16-le").rstrip("\0"), o - n * 2
            return b[o:o + n].decode("utf-8", "replace").rstrip("\0"), o + n

        self.mount, o = read_string(o)

        n_dirs = struct.unpack_from("<I", b, o)[0]; o += 4
        dirs = [struct.unpack_from("<4I", b, o + i * 16) for i in range(n_dirs)]
        o += n_dirs * 16

        n_files = struct.unpack_from("<I", b, o)[0]; o += 4
        files = [struct.unpack_from("<3I", b, o + i * 12) for i in range(n_files)]
        o += n_files * 12

        n_strings = struct.unpack_from("<I", b, o)[0]; o += 4
        strings = []
        for _ in range(n_strings):
            s, o = read_string(o)
            strings.append(s)

        self.dirs, self.files, self.strings = dirs, files, strings

        result = {}

        def walk(dir_index, prefix):
            while dir_index != INVALID:
                name_id, first_child, next_sibling, first_file = dirs[dir_index]
                name = strings[name_id] if name_id != INVALID else ""
                here = prefix + (("/" + name) if name else "")

                f = first_file
                while f != INVALID:
                    fname_id, next_file, chunk_index = files[f]
                    result[chunk_index] = (here + "/" + strings[fname_id]).lstrip("/")
                    f = next_file

                walk(first_child, here)
                dir_index = next_sibling

        walk(0, "")     # entry 0 is the unnamed root folder
        return result

    # -- reading chunks ------------------------------------------------------

    def read(self, index):
        """
        Return the fully decompressed bytes of chunk `index`.

        A chunk can span several 64KB blocks, so we walk blocks forward from the
        chunk's starting block until we've collected its whole length.
        """
        offset, length = self.offlen[index]
        block = offset // self.block_size      # virtual offset -> block number
        out = bytearray()
        remaining = length

        while remaining > 0:
            pos, csize, usize, method = self.blocks[block]
            self.ucas.seek(pos)
            raw = self.ucas.read(csize)
            out += raw[:usize] if method == 0 else oodle_decompress(raw, usize)
            remaining -= usize
            block += 1

        return bytes(out[:length])

    # -- small helpers -------------------------------------------------------

    def meta_hash(self, index):
        """The stored 32-byte checksum for a chunk (SHA-1, zero-padded)."""
        o = self.meta_off + index * 33
        return self.d[o:o + 32]

    def chunk_type(self, index):
        """2 = a package (.uasset), 10 = the container header."""
        return self.chunk_ids[index][11]

    def package_id(self, index):
        """For package chunks, the first 8 bytes of the chunk ID are its Package ID."""
        return struct.unpack_from("<Q", self.chunk_ids[index], 0)[0]

    def index_of(self, path):
        """Look up a chunk index by its path inside the mod."""
        for i, p in self.paths.items():
            if p == path:
                return i
        raise KeyError(path)


if __name__ == "__main__":
    # Quick self-test: list everything in the mod.
    toc = Toc(config.MOD_UTOC)
    print(f"version={toc.version} chunks={toc.n} flags={toc.flags:#x} "
          f"({'ENCRYPTED' if toc.flags & 2 else 'not encrypted'}, "
          f"{'SIGNED' if toc.flags & 4 else 'not signed'})")
    print(f"mount point: {toc.mount}")
    print(f"compression: {toc.methods}")
    for i in sorted(toc.paths):
        print(f"  [{i:3}] {toc.offlen[i][1]:>9} bytes  {toc.paths[i]}")
