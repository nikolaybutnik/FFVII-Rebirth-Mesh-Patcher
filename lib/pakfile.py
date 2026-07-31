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

Plugin paks in the wild additionally carry AccessTransformers.ini,
PluginSettings.ini and AssetRegistry.bin. Whether the loader actually requires
them is untested -- the assets it reads come from the container, not from here.
Writing real entries means the encoded-entry and path-hash formats too, so it is
deliberately not attempted until something is shown to need it.

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
