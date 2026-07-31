"""
pakfile.py -- writes the small .pak that sits beside a .utoc/.ucas pair.

WHY A CONTAINER STILL NEEDS A .pak
----------------------------------
The assets live entirely in the IoStore container. The .pak next to it carries
one thing that matters: the MOUNT POINT, the path the container's contents hang
off. A loose pak mod mounts at "../../../" (the install root); a plugin mounts
at "../../../End/Mods/<Name>/". Convert between the two formats and the mount
has to change with them, which is why the source .pak cannot simply be copied.

WHAT THIS WRITES
----------------
A version 11 pak holding NO files -- just a mount point and two empty indexes.
That is exactly what loose pak mods ship, so it is a shape the game is known to
accept, and building it is verified by reproducing one byte for byte.

Plugin paks additionally carry AccessTransformers.ini, PluginSettings.ini and
AssetRegistry.bin -- the registry is how the mod loader discovers a plugin's
data assets, so build_plugin writes the full shape: entries with local
headers, encoded entries, a path-hash index (FNV-1a-64 over the lowercased
UTF-16 path plus a seed -- verified against a real plugin pak's hashes) and a
directory index.

LAYOUT
------
    0                primary index: mount, counts, sub-index locations
    +                path hash index      (8 zero bytes when empty)
    +                full directory index (a single zero count)
    +                16-byte encryption GUID, then a bEncryptedIndex byte
    +                footer: magic, version, index offset/size/SHA-1,
                     then five 32-byte compression method names
"""

import hashlib
import struct

MAGIC = 0x5A6F12E1
VERSION = 11
COMPRESSION_SLOTS = 5
METHOD_NAME_LEN = 32

# Both sub-indexes serialize to a fixed, tiny blob when there is nothing in
# them. These are the exact bytes a real empty pak contains.
EMPTY_PATH_HASH_INDEX = b"\0" * 8
EMPTY_DIRECTORY_INDEX = b"\0" * 4


def _fstring(text):
    raw = text.encode("utf-8") + b"\0"
    return struct.pack("<i", len(raw)) + raw


def build(mount, methods=()):
    """
    Return the bytes of an empty pak mounted at `mount`.

    `methods` names the compression methods the container uses; they are
    recorded in the footer's fixed five slots. An empty pak compresses nothing,
    so this only matters for matching a source file exactly.
    """
    index_offset = 0
    head = _fstring(mount)
    head += struct.pack("<i", 0)                    # NumEntries
    head += struct.pack("<Q", 0)                    # PathHashSeed

    # The sub-indexes follow the primary index, so their offsets are known only
    # once its length is -- which is fixed, because both blobs are.
    fixed_tail = 4 + 8 + 8 + 20 + 4 + 8 + 8 + 20 + 4 + 4
    index_size = len(head) + fixed_tail
    ph_offset = index_offset + index_size
    fd_offset = ph_offset + len(EMPTY_PATH_HASH_INDEX)

    head += struct.pack("<i", 1)                    # has path hash index
    head += struct.pack("<qq", ph_offset, len(EMPTY_PATH_HASH_INDEX))
    head += hashlib.sha1(EMPTY_PATH_HASH_INDEX).digest()
    head += struct.pack("<i", 1)                    # has full directory index
    head += struct.pack("<qq", fd_offset, len(EMPTY_DIRECTORY_INDEX))
    head += hashlib.sha1(EMPTY_DIRECTORY_INDEX).digest()
    head += struct.pack("<i", 0)                    # encoded entries, none
    head += struct.pack("<i", 0)                    # non-encoded entries, none
    assert len(head) == index_size, (len(head), index_size)

    out = bytearray()
    out += head
    out += EMPTY_PATH_HASH_INDEX
    out += EMPTY_DIRECTORY_INDEX
    out += b"\0" * 16                               # encryption key GUID
    out += b"\0"                                    # bEncryptedIndex

    out += struct.pack("<II", MAGIC, VERSION)
    out += struct.pack("<qq", index_offset, index_size)
    out += hashlib.sha1(bytes(head)).digest()
    for slot in range(COMPRESSION_SLOTS):
        name = methods[slot].encode() if slot < len(methods) else b""
        out += name + b"\0" * (METHOD_NAME_LEN - len(name))
    return bytes(out)


def read_entries(data, decompressor=None):
    """
    Parse a pak written by build_plugin's conventions (or by the Dresscode
    toolchain): returns (mount, seed, [(path, plain bytes)]) in entry order.
    `decompressor(blob, out_size)` inflates compressed entries -- pass
    iostore.oodle_decompress. Raises on shapes this reader does not know.
    """
    pos = data.rfind(struct.pack("<I", MAGIC))
    if pos < 0:
        raise RuntimeError("not a pak file")
    ioff, isize = struct.unpack_from("<qq", data, pos + 8)
    p = ioff
    n = struct.unpack_from("<i", data, p)[0]
    mount = data[p + 4:p + 4 + n - 1].decode("utf-8")
    p += 4 + n
    count = struct.unpack_from("<i", data, p)[0]
    seed = struct.unpack_from("<Q", data, p + 4)[0]
    p += 12
    p += 4 + 16 + 20 + 4 + 16 + 20                  # both sub-index records
    esize = struct.unpack_from("<i", data, p)[0]
    p += 4
    encoded, offsets = data[p:p + esize], []
    o = 0
    while o < len(encoded):
        value = struct.unpack_from("<I", encoded, o)[0]
        # Keyed by the entry's offset WITHIN the encoded array -- that, not
        # the data offset, is what the directory index points at.
        offsets.append((o, struct.unpack_from("<I", encoded, o + 4)[0]))
        o += 12 + (4 if (value >> 23) & 0x3F else 0)

    # Paths come from the full directory index, keyed by entry offset.
    fd_off = struct.unpack_from("<q", data,
                                ioff + 4 + n + 12 + 4 + 16 + 20 + 4)[0]
    d = fd_off
    paths = {}
    ndirs = struct.unpack_from("<i", data, d)[0]
    d += 4
    for _ in range(ndirs):
        ln = struct.unpack_from("<i", data, d)[0]
        folder = data[d + 4:d + 4 + ln - 1].decode("utf-8")
        d += 4 + ln
        nfiles = struct.unpack_from("<i", data, d)[0]
        d += 4
        for _f in range(nfiles):
            ln = struct.unpack_from("<i", data, d)[0]
            fname = data[d + 4:d + 4 + ln - 1].decode("utf-8")
            d += 4 + ln
            off = struct.unpack_from("<I", data, d)[0]
            d += 4
            paths[off] = (folder.lstrip("/") + fname) if folder != "/" \
                else fname

    files = []
    for enc_off, off in offsets:
        _eo, size, usize, method = struct.unpack_from("<qqqI", data, off)
        if method == 0:
            body = data[off + 53:off + 53 + usize]
        else:
            nblocks = struct.unpack_from("<I", data, off + 48)[0]
            bsize = struct.unpack_from("<I", data,
                                       off + 52 + nblocks * 16 + 1)[0]
            body = b""
            o = off + 52
            for _ in range(nblocks):
                s, e = struct.unpack_from("<qq", data, o)
                o += 16
                body += decompressor(data[off + s:off + e],
                                     min(bsize, usize - len(body)))
        files.append((paths.get(enc_off, f"entry@{off}"), body))
    return mount, seed, files


def mount_of(data):
    """The mount point recorded in an existing pak, or None if unreadable."""
    pos = data.rfind(struct.pack("<I", MAGIC))
    if pos < 0:
        return None
    offset, size = struct.unpack_from("<qq", data, pos + 8)
    if offset + size > len(data):
        return None
    length = struct.unpack_from("<i", data, offset)[0]
    if length <= 0:
        return None
    return data[offset + 4:offset + 4 + length - 1].decode("utf-8", "replace")


# THE .pak MOUNT IS NOT THE .utoc MOUNT.
#
# The container's mount is a path prefix for the packages inside it, and is
# routinely deep -- ../../../End/Content/Character/Player/ is normal. The pak's
# mount is a content root the engine registers, and every working loose pak
# keeps it shallow: "/" alongside a deep container, "../../../" alongside a
# root-mounted one. Setting it to the container's deep path instead registers a
# bogus content root and the game dies on startup.
LOOSE_MOUNT = "/"


def plugin_mount(plugin):
    return f"../../../End/Mods/{plugin}/"


def _fnv64(data, seed):
    h = (0xcbf29ce484222325 + seed) & 0xFFFFFFFFFFFFFFFF
    for b in data:
        h ^= b
        h = (h * 0x00000100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


def path_hash(path, seed):
    """FPakFile::HashPath -- FNV-1a-64 over the lowercased UTF-16 path."""
    return _fnv64(path.lower().encode("utf-16-le"), seed)


def build_plugin(mount, files, compressor=None, seed=None):
    """
    A real plugin pak: `files` is [(path, data)], stored in order. `compressor`
    Oodle-compresses a blob (or returns None to store raw); when given, every
    entry that shrinks is compressed as a single block, which is how real
    plugin paks ship their AssetRegistry.bin. `seed` fixes the path-hash seed
    -- reproducing a specific pak needs its seed, a fresh one derives its own.
    """
    entries = bytearray()
    encoded = bytearray()
    records = []                                     # (path, encoded offset)

    for path, data in files:
        comp = compressor(data) if compressor else None
        if comp is not None and len(comp) < len(data):
            method, stored = 1, comp
        else:
            method, stored = 0, data
        entry_off = len(entries)
        records.append((path, len(encoded)))

        # The header copy in front of the data records its Offset as ZERO --
        # the loader compares this copy against the index entry on every
        # read, with the offset taken as entry-relative, and a mismatch is a
        # fatal "pak entry mismatch" at startup.
        header = struct.pack("<qqqI", 0, len(stored), len(data), method)
        header += hashlib.sha1(stored).digest()
        if method:
            head_len = 48 + 4 + 16 + 5
            header += struct.pack("<I", 1)           # one block
            header += struct.pack("<qq", head_len, head_len + len(stored))
            header += b"\0" + struct.pack("<I", len(data))
        else:
            header += b"\0" + struct.pack("<I", 0)
        entries += header + stored

        value = (1 << 31) | (1 << 30) | (1 << 29)    # all sizes 32-bit safe
        if method:
            value |= (method << 23) | (1 << 6) | ((len(data) >> 11) & 0x3F)
        encoded += struct.pack("<I", value)
        encoded += struct.pack("<II", entry_off, len(data))
        if method:
            encoded += struct.pack("<I", len(stored))

    # Directory tree: files at the root live in "/", the rest in "<dir>/".
    dirs = {}
    for path, off in records:
        folder, _, name = path.rpartition("/")
        dirs.setdefault(folder + "/" if folder else "/", []).append((name, off))

    def dir_blob(keep):
        blob = struct.pack("<i", len(dirs))
        for folder in sorted(dirs, key=lambda f: (f != "/", f)):
            kept = [(n, o) for n, o in dirs[folder] if keep(n)]
            blob += _fstring(folder)
            blob += struct.pack("<i", len(kept))
            for name, off in kept:
                blob += _fstring(name) + struct.pack("<I", off)
        return blob

    dir_index = dir_blob(lambda n: True)

    if seed is None:
        seed = _fnv64(mount.lower().encode("utf-16-le"), 0) & 0xFFFFFFFF
    ph_index = struct.pack("<i", len(records))
    for path, off in records:
        ph_index += struct.pack("<QI", path_hash(path, seed), off)
    # The embedded copy is PRUNED: real plugin paks keep only the .ini files
    # in it, dropping the registry from the memory-resident directory.
    ph_index += dir_blob(lambda n: n.lower().endswith(".ini"))

    head = _fstring(mount)
    head += struct.pack("<i", len(records))
    head += struct.pack("<Q", seed)
    fixed_tail = 4 + 8 + 8 + 20 + 4 + 8 + 8 + 20 + 4 + len(encoded) + 4
    index_offset = len(entries)
    index_size = len(head) + fixed_tail
    ph_offset = index_offset + index_size
    fd_offset = ph_offset + len(ph_index)

    head += struct.pack("<i", 1)
    head += struct.pack("<qq", ph_offset, len(ph_index))
    head += hashlib.sha1(ph_index).digest()
    head += struct.pack("<i", 1)
    head += struct.pack("<qq", fd_offset, len(dir_index))
    head += hashlib.sha1(dir_index).digest()
    head += struct.pack("<i", len(encoded)) + bytes(encoded)
    head += struct.pack("<i", 0)                     # non-encoded entries
    assert len(head) == index_size, (len(head), index_size)

    out = bytearray()
    out += entries
    out += head
    out += ph_index
    out += dir_index
    out += b"\0" * 16                                # encryption key GUID
    out += b"\0"                                     # bEncryptedIndex
    out += struct.pack("<II", MAGIC, VERSION)
    out += struct.pack("<qq", index_offset, index_size)
    out += hashlib.sha1(bytes(head)).digest()
    for slot in range(COMPRESSION_SLOTS):
        name = b"oodle" if slot == 0 else b""
        out += name + b"\0" * (METHOD_NAME_LEN - len(name))
    return bytes(out)
