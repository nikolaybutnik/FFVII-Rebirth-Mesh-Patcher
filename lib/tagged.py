"""
tagged.py -- reads Unreal's self-describing "tagged" property format.

Mods are cooked with tagged properties, where every value is preceded by its
name, its type and its length. The game's own assets are not -- V1.005 recooked
them to the unversioned format, which needs an external schema to read at all.
That difference is why the loader's data assets can be understood here with no
mapping file: they come from a mod.

A property is:

    FName  Name          ("None" ends the list)
    FName  Type          ("StrProperty", "ArrayProperty", ...)
    int32  Size          bytes of value that follow the tag
    int32  ArrayIndex
    ...    type-specific tag data (struct name + GUID, array inner type, ...)
    uint8  HasPropertyGuid
    Size bytes of value

Sizes are exact, so an unknown type can always be stepped over -- which is what
makes a partial reader safe.
"""

import struct


class Reader:
    """A cursor over a package's export data, resolving names through it."""

    def __init__(self, data, offset, pkg):
        self.d = data
        self.o = offset
        self.pkg = pkg

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.o)[0]
        self.o += 4
        return v

    def u8(self):
        v = self.d[self.o]
        self.o += 1
        return v

    def name(self):
        i, n = struct.unpack_from("<II", self.d, self.o)
        self.o += 8
        return self.pkg.name_at(i, n)

    def string(self):
        """An FString: length-prefixed, negative length meaning UTF-16."""
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            s = self.d[self.o:self.o - n * 2 - 2].decode("utf-16-le")
            self.o += -n * 2
        else:
            s = self.d[self.o:self.o + n - 1].decode("utf-8", "replace")
            self.o += n
        return s

    def soft_object_path(self):
        """FName asset path plus an FString subpath; "None" means unset."""
        path = self.name()
        sub = self.string()
        if path == "None":
            return None
        return f"{path}:{sub}" if sub else path


def read_properties(r, limit):
    """
    Read one property list into {name: value}, stopping at "None" or `limit`.

    Values are decoded for the types the loader's data assets actually use;
    anything else comes back as raw bytes, still correctly delimited.
    """
    out = {}
    while r.o < limit - 8:
        tag = r.name()
        if tag == "None":
            break
        typ = r.name()
        size = r.i32()
        r.i32()                                     # ArrayIndex

        meta = None
        if typ == "StructProperty":
            meta = r.name()
            r.o += 16                               # struct GUID
        elif typ == "ArrayProperty":
            meta = r.name()
        elif typ in ("EnumProperty", "ByteProperty"):
            meta = r.name()
        elif typ == "MapProperty":
            meta = (r.name(), r.name())
        elif typ == "BoolProperty":
            meta = bool(r.u8())
        r.u8()                                      # HasPropertyGuid

        end = r.o + size
        out[tag] = _read_value(r, typ, meta, end)
        r.o = end
    return out


def _read_value(r, typ, meta, end):
    if typ == "StrProperty":
        return r.string()
    if typ in ("NameProperty", "EnumProperty"):
        return r.name()
    if typ == "BoolProperty":
        return meta                                 # the value lives in the tag
    if typ == "ObjectProperty":
        return r.i32()
    if typ == "StructProperty":
        if meta == "SoftObjectPath":
            return r.soft_object_path()
        return read_properties(r, end)
    if typ == "ArrayProperty":
        return _read_array(r, meta, end)
    return r.d[r.o:end]


def _read_array(r, inner, end):
    count = r.i32()
    if inner != "StructProperty":
        # Only struct arrays carry an inner tag; everything else is packed
        # values, which callers decode themselves if they need to.
        return r.d[r.o:end]
    r.name(); r.name(); r.i32(); r.i32()            # inner tag: name, type, size, index
    r.name()                                        # struct name
    r.o += 16                                       # struct GUID
    r.u8()                                          # HasPropertyGuid
    return [read_properties(r, end) for _ in range(count)]


def base_field(name):
    """
    Strip a blueprint property's GUID suffix: "Name_10_E3B3..." -> "Name".

    Blueprint-authored structs suffix every field with its ordinal and a GUID,
    which is stable per loader build but ugly and version-specific. Matching on
    the base name keeps callers readable.
    """
    parts = name.split("_")
    while len(parts) > 1 and (parts[-1].isdigit() or len(parts[-1]) >= 16):
        parts.pop()
    return "_".join(parts)


def by_base(props):
    """Re-key a property dict by base field name."""
    return {base_field(k): v for k, v in props.items()}
