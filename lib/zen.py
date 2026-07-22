"""
zen.py -- reads a single Unreal package (one .uasset) in "Zen" container format.

WHAT A PACKAGE IS
-----------------
Every asset in the game -- a 3D model, a texture, a UI screen, a script -- lives in
a package. Once you've pulled a chunk out of the container with iostore.py, this
module makes sense of the bytes.

A package has three parts that matter to us:

  NAME TABLE  Every piece of text used by the package, stored once. Everything
              else refers to text by number rather than repeating it.

  IMPORTS     Things this package USES from other packages. If a blueprint places
              a mesh in the world, it has an import pointing at that mesh.
              >>> This is what we edit to sever the PointerCrystal reference. <<<

  EXPORTS     Things this package DEFINES. Empty.uasset exports one object called
              "Empty" whose class is SkeletalMesh. Scanning every package's exports
              for class == SkeletalMesh is how the two broken meshes were found.

"""

import struct


def load_name_batch(namedata, hashdata):
    """
    Decode a package's name table into a list of strings.

    Two things about this format are genuinely surprising, and both cost me a
    debugging round:

    1. The NUMBER of names is not stored in the name data. You compute it from
       the size of the separate *hash* blob:  count = len(hashdata)/8 - 1

    2. Headers and strings are INTERLEAVED -- header, string, header, string --
       and each 2-byte header is packed big-endian-style:

           is_utf16 = byte0 >> 7
           length   = ((byte0 & 0x7f) << 8) | byte1

       Some Unreal versions store all headers first and then all strings. If you
       assume that here you get plausible-looking but subtly shifted garbage,
       which is the worst kind of bug to chase. A raw hex dump makes it obvious:

           00 26 "/Dresscode/Assets/Empty/Empty_Skeleton"    <- 0x26 = 38 chars
           00 13 "/Script/CoreUObject"                       <- 0x13 = 19 chars
    """
    if not hashdata or len(hashdata) < 8:
        return []

    count = len(hashdata) // 8 - 1
    o = 0
    names = []

    for _ in range(count):
        b0, b1 = namedata[o], namedata[o + 1]
        o += 2
        is_utf16 = b0 >> 7
        length = ((b0 & 0x7F) << 8) | b1
        if is_utf16:
            # Wide names are big-endian and padded to a 2-byte boundary; missing
            # the pad desyncs the rest of the table.
            names.append(namedata[o:o + length * 2].decode("utf-16-be", "replace"))
            o += length * 2
            if o & 1:
                o += 1
        else:
            names.append(namedata[o:o + length].decode("utf-8", "replace"))
            o += length

    return names


class ZenPackage:
    """
    One parsed package.

        pkg = ZenPackage(toc.read(4))
        for e in pkg.exports:
            print(e["name"], hex(e["cls"]))

    Attributes:
        names       the name table (list of strings)
        imports     list of 8-byte IDs referring to objects in OTHER packages
        exports     list of dicts describing objects this package defines
        imp_off     byte offset of the import table (needed to patch imports)
        pkg_flags   0x80000000 = Cooked, 0x00002000 = UnversionedProperties
    """

    # Package summary header. Field order matters.
    HEADER = "<QQIIiiiiiiiiii"

    def __init__(self, data):
        self.d = data

        (self.name, self.srcname, self.pkg_flags, self.cooked_hdr_size,
         self.nm_off, self.nm_size,          # name strings blob
         self.nh_off, self.nh_size,          # name hashes blob
         self.imp_off, self.exp_off, self.bundles_off,
         self.graph_off, self.graph_size,
         self.pad) = struct.unpack_from(self.HEADER, data, 0)

        self.names = load_name_batch(data[self.nm_off:self.nm_off + self.nm_size],
                                     data[self.nh_off:self.nh_off + self.nh_size])

        # --- Imports: a flat array of 8-byte IDs, running from the import table
        #     offset up to where the export table begins.
        #
        #     Top 2 bits are a type tag; type 3 means Null, so a "points at
        #     nothing" import is 0xFFFFFFFFFFFFFFFF.
        n_imports = (self.exp_off - self.imp_off) // 8
        self.imports = (list(struct.unpack_from(f"<{n_imports}Q", data, self.imp_off))
                        if n_imports > 0 else [])

        # --- Exports: 72 bytes each. ---
        n_exports = (self.bundles_off - self.exp_off) // 72
        self.exports = []
        for i in range(n_exports):
            o = self.exp_off + i * 72
            (serial_off, serial_size, name_i, name_n, outer,
             cls, super_, template, global_import,
             obj_flags) = struct.unpack_from("<QQIIQQQQQI", data, o)

            self.exports.append(dict(
                idx=i,
                name=self.name_at(name_i, name_n),
                off=serial_off,       # offset in the ORIGINAL pre-container file
                size=serial_size,     # byte length of this object's data
                outer=outer,
                cls=cls,              # class ID -- resolve via globals_meta.py
                super=super_,
                tmpl=template,
                # The ID other packages use when importing THIS object. Matching
                # against this is how we find a reference without needing to
                # reimplement Unreal's CityHash64 path hashing.
                gimp=global_import,
                flags=obj_flags,
                filt=data[o + 68],
            ))

    def name_at(self, index, number=0):
        """
        Resolve a name reference.

        Names are stored as (index, number). The top 2 bits of the index are a
        type tag and must be masked off. number == 0 means use the name as-is;
        otherwise Unreal appends _(number-1), so ("Foo", 3) becomes "Foo_2".
        """
        index &= 0x3FFFFFFF
        s = self.names[index] if index < len(self.names) else f"<bad:{index}>"
        return s if number == 0 else f"{s}_{number - 1}"


    def export_data_start(self):
        """
        Where the actual object data begins inside this package.

        Note we do NOT use an export's `off` field for this -- that refers to a
        position in the original pre-container file, not in these bytes. The real
        data starts right after the graph data, with exports laid out one after
        another in order.
        """
        return self.graph_off + self.graph_size

    def find_export_payload(self, class_id):
        """
        Locate the data belonging to the first export of the given class.

        Returns (start, end) byte offsets within this package, or None.
        """
        offset = self.export_data_start()
        for e in self.exports:
            if e["cls"] == class_id:
                return offset, offset + e["size"]
            offset += e["size"]
        return None

    def uses_unversioned_properties(self):
        """
        True if this package uses the compact "unversioned" property format.

        All of the game's packages set this; none of the mod's do. That is a real
        difference -- but it is NOT what patch V1.005 broke. It predates the patch,
        the mod worked fine with it, and Unreal handles both styles per package.
        Recorded here so nobody wastes time chasing it (as I nearly did).
        """
        return bool(self.pkg_flags & 0x00002000)
