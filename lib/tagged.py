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


# ---------------------------------------------------------------------------
# Writing. The mirror of the reader above, for building a Dresscode mod's
# registration assets from scratch. A property list is modelled as
# [(name, spec), ...] where spec is one of:
#
#     ("str", text)                        StrProperty
#     ("name", text)                       NameProperty
#     ("obj", package_index)               ObjectProperty (FPackageIndex)
#     ("enum", enum_type, value_name)      EnumProperty
#     ("byte_enum", enum_type, value_name)  ByteProperty holding an enum FName
#                                          (blueprint enum class properties,
#                                          e.g. Dresscode's "Mod Type")
#     ("bool", value)                      BoolProperty (value in the tag)
#     ("softpath", "/Pkg/A.A" or None)     StructProperty<SoftObjectPath>
#     ("struct", type_name, guid16, props) StructProperty
#     ("struct_raw", type_name, bytes)     StructProperty, opaque value
#     ("array_structs", type_name, guid16, [props, ...])
#     ("map", key_type, value_type)        MapProperty, empty
#     ("map", "NameProperty", "ObjectProperty", [(name, package_index), ...])
#
# Emitting needs every FName's final table index, and the table must exist
# before anything can be emitted -- so building is two passes: collect_names
# gathers every string a property list will intern, emit_properties writes the
# bytes once the caller can resolve them.
# ---------------------------------------------------------------------------

TERMINATOR = "None"


def collect_names(props, out):
    """Add every FName `props` will reference to the set `out`."""
    for name, spec in props:
        kind = spec[0]
        out.add(name)
        out.add({"str": "StrProperty", "obj": "ObjectProperty",
                 "name": "NameProperty",
                 "enum": "EnumProperty", "byte_enum": "ByteProperty",
                 "bool": "BoolProperty",
                 "softpath": "StructProperty",
                 "struct": "StructProperty", "struct_raw": "StructProperty",
                 "array_structs": "ArrayProperty",
                 "map": "MapProperty"}[kind])
        if kind == "name":
            out.add(spec[1])
        elif kind in ("enum", "byte_enum"):
            out.add(spec[1])
            out.add(spec[2])
        elif kind == "softpath":
            out.add("SoftObjectPath")
            if spec[1]:
                out.add(spec[1])
                out.add(spec[1].split(".")[0])  # cooker interns the bare
            else:                               # package path too
                out.add(TERMINATOR)
        elif kind == "struct":
            out.add(spec[1])
            collect_names(spec[3], out)
        elif kind == "struct_raw":
            out.add(spec[1])
        elif kind == "array_structs":
            out.add("StructProperty")
            out.add(spec[1])
            for body in spec[3]:
                collect_names(body, out)
        elif kind == "map":
            out.add(spec[1])
            out.add(spec[2])
            for key, _value in (spec[3] if len(spec) > 3 else ()):
                out.add(key)
    out.add(TERMINATOR)


def _fstring(text):
    if not text:
        return struct.pack("<i", 0)
    raw = text.encode("utf-8") + b"\0"
    return struct.pack("<i", len(raw)) + raw


def emit_properties(props, name_of):
    """The property list as bytes, TERMINATOR excluded -- nested lists get
    theirs, the caller of the outermost list adds its own."""
    def fname(text):
        return struct.pack("<II", name_of(text), 0)

    def tag(name, typ, value, extra=b""):
        return (fname(name) + fname(typ)
                + struct.pack("<ii", len(value), 0) + extra + b"\0" + value)

    out = b""
    for name, spec in props:
        kind = spec[0]
        if kind == "str":
            out += tag(name, "StrProperty", _fstring(spec[1]))
        elif kind == "name":
            out += tag(name, "NameProperty", fname(spec[1]))
        elif kind == "obj":
            out += tag(name, "ObjectProperty", struct.pack("<i", spec[1]))
        elif kind == "enum":
            out += tag(name, "EnumProperty", fname(spec[2]), fname(spec[1]))
        elif kind == "byte_enum":
            out += tag(name, "ByteProperty", fname(spec[2]), fname(spec[1]))
        elif kind == "bool":
            out += tag(name, "BoolProperty", b"",
                       b"\x01" if spec[1] else b"\x00")
        elif kind == "struct_raw":
            out += tag(name, "StructProperty", spec[2],
                       fname(spec[1]) + b"\0" * 16)
        elif kind == "softpath":
            value = fname(spec[1] or TERMINATOR) + struct.pack("<i", 0)
            out += tag(name, "StructProperty", value,
                       fname("SoftObjectPath") + b"\0" * 16)
        elif kind == "struct":
            body = emit_properties(spec[3], name_of) + fname(TERMINATOR)
            out += tag(name, "StructProperty", body,
                       fname(spec[1]) + spec[2])
        elif kind == "array_structs":
            bodies = b"".join(emit_properties(b, name_of) + fname(TERMINATOR)
                              for b in spec[3])
            inner = (fname(name) + fname("StructProperty")
                     + struct.pack("<ii", len(bodies), 0)
                     + fname(spec[1]) + spec[2] + b"\0")
            value = struct.pack("<i", len(spec[3])) + inner + bodies
            out += tag(name, "ArrayProperty", value, fname("StructProperty"))
        elif kind == "map":
            # Pairs, when given, are Name -> FPackageIndex; the map's own
            # iteration order is the caller's, as the loader rebuilds it.
            pairs = spec[3] if len(spec) > 3 else ()
            body = b"".join(fname(k) + struct.pack("<i", v) for k, v in pairs)
            value = struct.pack("<ii", 0, len(pairs)) + body
            out += tag(name, "MapProperty", value,
                       fname(spec[1]) + fname(spec[2]))
        else:
            raise ValueError(f"unknown property spec {kind!r}")
    return out


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
