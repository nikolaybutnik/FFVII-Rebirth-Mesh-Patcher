"""
moddata.py -- reads the two data assets that make a mod a Dresscode mod.

A Dresscode costume is an ordinary cooked plugin plus two registration assets:

  DA_ModMetaData          the mod-list card: friendly name, author, category,
                          thumbnail. An instance of FF7RML's PDA_ModMetaData.
  PDA_ModData_Character   the outfits themselves -- one array entry per
                          selectable costume, each naming a skeletal mesh, the
                          character it belongs to, and a preview image.

Everything else in the container is the costume's actual assets, and looks like
any other cooked content.

WHERE THE FIELD NAMES COME FROM
-------------------------------
Blueprint structs suffix every field with an ordinal and a GUID
(Name_10_E3B347814D726EDE0AD7349893E85FE6). Those suffixes are fixed for a given
FF7RML build, and FF7RML ships the struct definitions themselves -- so they can
be read from the installed loader rather than copied out of somebody's mod.
tagged.base_field strips them, which is why nothing here hardcodes one.
"""

import cityhash
import tagged
from zen import ZenPackage

CHARACTER_DATA = "Character Data"

# Package paths inside FF7RML that define the two assets' classes.
METADATA_CLASS = "/FF7RML/ModLoaders/Structs/PDA_ModMetaData"
CHARACTER_CLASS = ("/FF7RML/ModLoaders/Extensions/FF7RDataLibrary/Structs/"
                   "PDA_ModData_Character")

# EPlayerType enumerator <-> the stock costume folder that character wears.
# Verified against the game's own Character/Player tree; Vincent has the folder
# but no mod has yet named him, so his enumerator is inferred from the pattern.
PLAYER_TYPES = {
    "CLOUD": ("PC0000", "PC0000_00_Cloud_Standard"),
    "BARRET": ("PC0001", "PC0001_00_Barret_Standard"),
    "TIFA": ("PC0002", "PC0002_00_Tifa_Standard"),
    "AERITH": ("PC0003", "PC0003_00_Aerith_Standard"),
    "REDXIII": ("PC0004", "PC0004_00_RedXIII_Standard"),
    "YUFFIE": ("PC0005", "PC0005_00_Yuffie_Standard"),
    "CAITSITH": ("PC0007", "PC0007_00_CaitSith_Standard"),
    "ZACK": ("PC0009", "PC0009_00_Zack_Standard"),
    "SEPHIROTH": ("PC0010", "PC0010_00_Sephiroth_Standard"),
    "VINCENT": ("PC0011", "PC0011_00_Vincent_Standard"),
}


def default_costume_package(player_type):
    """
    The stock package a costume replaces when converted to a pak.

    Always the character's default outfit -- /Game/.../PC00XX_00_Name_Standard/
    Model/PC00XX_00. Restricting to the default is what lets a conversion round
    trip: any other choice would have to be remembered somewhere.
    """
    key = player_type.split("::")[-1].upper()
    if key not in PLAYER_TYPES:
        return None
    prefix, folder = PLAYER_TYPES[key]
    return f"/Game/Character/Player/{folder}/Model/{prefix}_00"


def player_type_for_package(package_name):
    """The inverse: which character a stock costume package belongs to."""
    low = package_name.lower()
    for key, (_prefix, folder) in PLAYER_TYPES.items():
        if f"/player/{folder.lower()}/" in low:
            return f"EPlayerType::{key}"
    return None


def _properties(data):
    pkg = ZenPackage(data)
    r = tagged.Reader(data, pkg.export_data_start(), pkg)
    return tagged.by_base(tagged.read_properties(r, len(data))), pkg


def read_mod_metadata(data):
    """{friendly_name, description, category, created_by, thumbnail} or {}."""
    props, _ = _properties(data)
    meta = tagged.by_base(props.get("MetaData") or {})
    if not meta:
        return {}
    return dict(
        friendly_name=meta.get("FriendlyName", ""),
        description=meta.get("Description", ""),
        category=meta.get("Category", ""),
        created_by=meta.get("CreatedBy", ""),
        version_name=meta.get("VersionName", ""),
        thumbnail=meta.get("Thumbnail"),
    )


def read_outfits(data):
    """
    Every costume the mod registers, in menu order.

    Each is {name, description, preview_image, player_type, skeletal_mesh,
    actor}, with soft object paths left as written ("/Mod/Assets/X.X").
    """
    props, _ = _properties(data)
    out = []
    for entry in props.get(CHARACTER_DATA) or []:
        entry = tagged.by_base(entry)
        general = tagged.by_base(entry.get("GeneralData") or {})
        mesh = tagged.by_base(entry.get("SkeletalMeshData") or {})
        out.append(dict(
            name=general.get("Name", ""),
            description=general.get("Description", ""),
            preview_image=general.get("PreviewImage"),
            player_type=mesh.get("PlayerType", ""),
            skeletal_mesh=mesh.get("SkeletalMesh"),
            actor=mesh.get("Actor"),
        ))
    return out


def _is_menu_list(data):
    """An asset with a top-level "Mod Type" is an auxiliary menu list (rows
    for Dresscode's WEAPONS menu), never the mod's own costume data."""
    try:
        props, _ = _properties(data)
        return "Mod Type" in props
    except Exception:
        return False


def _registration_matches(toc):
    """(kind, chunk index, package name) for every registration-class
    instance in the container."""
    want = {
        cityhash.object_id(METADATA_CLASS, "PDA_ModMetaData_C"): "metadata",
        cityhash.object_id(CHARACTER_CLASS, "PDA_ModData_Character_C"): "character",
    }
    out = []
    for i in range(toc.n):
        if toc.chunk_ids[i][11] != 2:
            continue
        try:
            pkg = ZenPackage(toc.read(i))
        except Exception:
            continue
        own = pkg.names[pkg.name & 0x3FFFFFFF] if pkg.names else ""
        for e in pkg.exports:
            kind = want.get(e["cls"])
            if kind:
                out.append((kind, i, own))
    return out


def find_data_assets(toc, prefer=None):
    """
    Locate the mod's two registration assets by the class they instantiate.

    Matching on the class -- via the global import ID of FF7RML's own asset --
    rather than on a filename, because authors put them wherever they like:
    MetaData/, Metadata/ and Assets/ all occur in the wild.

    `prefer` is a lowercase package-name prefix: a container merged with a
    library mod holds TWO sets of registration assets, and the ones under
    the mod's own root are the ones that define it. A weapon-tile asset
    (same class, "Mod Type" set) is never returned as the costume data.
    """
    found, fallback = {}, {}
    for kind, i, own in _registration_matches(toc):
        if kind == "character" and _is_menu_list(toc.read(i)):
            continue
        if prefer and own.lower().startswith(prefer):
            found[kind] = i
        else:
            fallback[kind] = i
    return {**fallback, **found}


def registration_chunks(toc):
    """
    Chunk index of EVERY registration-class instance -- the costume data,
    the mod card, and any auxiliary menu list. This is the drop set for a
    conversion to a loose pak: these are the only packages that import the
    FF7RML plugin, which does not exist yet when ~mods mounts, and one
    surviving the conversion crashes the game at startup.
    """
    return {i for _kind, i, _own in _registration_matches(toc)}
