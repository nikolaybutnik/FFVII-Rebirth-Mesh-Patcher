"""
dirindex.py -- builds the directory index (the filename table) for a .utoc.

WHAT THIS IS FOR
----------------
The container stores its file listing as a TREE, not a flat list of paths. Rather
than storing "Assets/Empty/Empty.uasset" as one string, it stores:

    root
     +- "Assets"
         +- "Empty"
             +- file "Empty.uasset"  -> chunk 4

We must rebuild this whenever the file list changes. Removing two packages doesn't
just delete two entries -- it also shifts the chunk numbers of everything after
them, and each file entry stores the chunk number it points at.

HOW THE TREE IS STORED
----------------------
Not as nested arrays but as LINKED LISTS, which is unusual if you haven't seen it:

  Each folder stores  -> its FIRST child folder, and its NEXT SIBLING folder
  Each folder stores  -> its FIRST file
  Each file stores    -> the NEXT file in that same folder

0xFFFFFFFF means "nothing here / end of list". To list a folder's contents you
follow its first-child pointer, then hop sibling to sibling until you hit
0xFFFFFFFF.

ONE SUBTLETY -- LISTS ARE BUILT BACKWARDS
-----------------------------------------
New entries are PREPENDED to their list, not appended. So each list ends up in
reverse creation order: the last folder added becomes its parent's first child.

Folder *numbers* are still assigned in creation order -- it's only the links that
run backwards. In this mod the root's children chain is 9 -> 6 -> 5 -> 4 -> 3 -> 1,
which is exactly the reverse of the order they were created in.

Appending instead of prepending still produces a perfectly valid, working tree --
just a differently ordered one. We prepend so the output matches Unreal's own
byte for byte, which is what makes the roundtrip test in verify.py meaningful.

Verified correct: rebuilding the untouched file list reproduces the original
index byte for byte (2380 bytes).

"""

import struct

INVALID = 0xFFFFFFFF


def build_dir_index(mount, files):
    """
    Serialize a directory index.

        mount  path prefix, e.g. "../../../End/Mods/Dresscode/Content/"
        files  list of (path, chunk_index) pairs

    Returns the raw bytes to drop into the .utoc.
    """
    # --- String table. Every folder and file name is stored once here and
    #     referenced by number, so repeated names cost nothing.
    strings = []
    string_ids = {}

    def string_id(s):
        if s not in string_ids:
            string_ids[s] = len(strings)
            strings.append(s)
        return string_ids[s]

    # Folder node layout: [name_id, first_child, next_sibling, first_file]
    # Entry 0 is the root, which has no name.
    dirs = [[INVALID, INVALID, INVALID, INVALID]]
    file_entries = []                # [name_id, next_file, chunk_index]

    children = {}       # parent folder -> {name: folder index}

    def get_or_make_dir(parent, name):
        """Find the named subfolder of `parent`, creating it if needed."""
        siblings = children.setdefault(parent, {})
        if name in siblings:
            return siblings[name]

        index = len(dirs)
        dirs.append([string_id(name), INVALID, INVALID, INVALID])
        siblings[name] = index

        # PREPEND: the new folder takes over as the parent's first child, and
        # points at whoever held that slot before. See the note at the top.
        dirs[index][2] = dirs[parent][1]     # new.next_sibling = old first child
        dirs[parent][1] = index              # parent.first_child = new
        return index

    for path, chunk_index in files:
        parts = path.strip("/").split("/")

        # Walk/create the folder chain, then attach the file to the last folder.
        folder = 0
        for segment in parts[:-1]:
            folder = get_or_make_dir(folder, segment)

        entry = len(file_entries)
        # PREPEND, same as folders.
        file_entries.append([string_id(parts[-1]), dirs[folder][3], chunk_index])
        dirs[folder][3] = entry

    # --- Serialize ---------------------------------------------------------
    out = bytearray()

    # Strings here are length-prefixed and INCLUDE their null terminator.
    m = mount.encode("utf-8") + b"\x00"
    out += struct.pack("<i", len(m)) + m

    out += struct.pack("<I", len(dirs))
    for e in dirs:
        out += struct.pack("<4I", *e)

    out += struct.pack("<I", len(file_entries))
    for e in file_entries:
        out += struct.pack("<3I", *e)

    out += struct.pack("<I", len(strings))
    for s in strings:
        b = s.encode("utf-8") + b"\x00"
        out += struct.pack("<i", len(b)) + b

    return bytes(out)
