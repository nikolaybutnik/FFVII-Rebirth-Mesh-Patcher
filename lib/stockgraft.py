"""
stockgraft.py -- keep a loose pak's stock-texture retouches working in a plugin.

Some loose mods ship retouched copies of stock textures (skin colour,
occlusion) beside their outfit. Nothing in the pak imports them: as a loose
pak they work by overriding the game's own package, so the game's skin
material picks up the retouch. A Dresscode plugin renames every package under
/<Mod>/ -- a plugin carrying /Game/ paths was fatal at startup when mounted,
and none of 95 shipping Dresscode containers does it -- which orphans such
retouches: the stock material keeps sampling the stock texture, and the
outfit shows the vanilla shading the author painted out (baked-in clothing
lines on bare skin, typically).

So the retouch rides the outfit instead. The pak's own header names its stock
imports, and the game containers' headers name what each of those imports in
turn -- two lookups link the orphaned texture to the stock material sampling
it. That material is copied out of the game's containers and renamed into the
plugin with everything else; the ordinary rewrite then repoints
mesh -> material -> texture, and the retouch applies exactly while the outfit
is worn -- the only time this mesh could show it anyway.
"""

import glob
import os
import struct

import cityhash
import config
import conheader
import iostore
import pkgedit
import rename
from zen import ZenPackage

PACKAGE_CHUNK = 2
BULK_CHUNKS = (3, 4)
HEADER_CHUNK = 10

_blobs = {}      # utoc -> (chunk count, raw chunk-id array)
_tocs = {}       # utoc -> open Toc
_headers = {}    # utoc -> (header bytes, info, {pid: store index}) or None


def _utocs():
    paks = getattr(config, "GAME_PAKS", "")
    if not paks or not os.path.isdir(paks):
        return []
    return sorted(glob.glob(os.path.join(paks, "*.utoc")))


def _ids(u):
    got = _blobs.get(u)
    if got is None:
        got = (0, b"")
        try:
            with open(u, "rb") as f:
                head = f.read(0x20)
                if head[:16] == b"-==--==--==--==-":
                    hdr_size, n = struct.unpack_from("<2I", head, 0x14)
                    f.seek(hdr_size)
                    got = (n, f.read(n * 12))
        except OSError:
            pass
        _blobs[u] = got
    return got


def _locate(wanted):
    """{pid: dict(pkg=(utoc, chunk index), bulks=[...])} for every wanted
    package id the game's own containers hold. Chunk-id arrays only -- no
    container is opened, so a miss costs one file read per .utoc."""
    out = {}
    for u in _utocs():
        n, blob = _ids(u)
        for k in range(n):
            pid = struct.unpack_from("<Q", blob, k * 12)[0]
            if pid not in wanted:
                continue
            t = blob[k * 12 + 11]
            e = out.setdefault(pid, dict(pkg=None, bulks=[]))
            if t == PACKAGE_CHUNK and e["pkg"] is None:
                e["pkg"] = (u, k)
            elif t in BULK_CHUNKS:
                e["bulks"].append((u, k))
    return out


def _toc(u):
    t = _tocs.get(u)
    if t is None:
        t = _tocs[u] = iostore.Toc(u)
    return t


def _header(u):
    got = _headers.get(u, False)
    if got is False:
        got = None
        try:
            toc = _toc(u)
            i = next((i for i in range(toc.n)
                      if toc.chunk_ids[i][11] == HEADER_CHUNK), None)
            if i is not None:
                raw = toc.read(i)
                info = conheader.parse(raw)
                if info:
                    ids = conheader.package_ids(raw, info)
                    got = (raw, info, {pid: j for j, pid in enumerate(ids)})
        except Exception:
            got = None
        _headers[u] = got
    return got


def _entry(pid, place):
    """(exports, bundles, imported pids) for a game package, from the store
    entry of the container that serves it."""
    got = _header(place["pkg"][0])
    if not got:
        return None
    raw, info, index = got
    j = index.get(pid)
    if j is None:
        return None
    _sz, exp, bun = struct.unpack_from(
        "<Qii", raw, info["store_off"] + j * 32)[:3]
    return exp, bun, conheader.imported_packages(raw, info, j)


def plan(packages, raw_meta, mesh_pids, say=print):
    """
    The stock packages a plugin must carry so this pak's stock-texture
    retouches keep working.

    `packages` is rename.read_packages of the pak, `raw_meta` its header's
    {pid: (exports, bundles, imported pids)}, `mesh_pids` the packages
    becoming outfits (unreferenced by design, never retouches).

    Returns {pid: dict(name, data, exp, bun, deps, bulks)} -- {} when the pak
    retouches nothing. `bulks` entries are (12-byte chunk id, payload).
    """
    imported = set()
    for _exp, _bun, deps in raw_meta.values():
        imported.update(deps)
    if not imported:
        return {}
    orphans = {pid: k["name"] for pid, k in packages.items()
               if k["name"].lower().startswith("/game/")
               and pid not in imported and pid not in mesh_pids}
    # A model's condition (petrify) variant is soft-referenced from the
    # mesh's UserData strings, which never reach the container header -- it
    # looks unreferenced but is part of the outfit. The ordinary rename
    # carries it and rewrites the soft reference, so it keeps working.
    orphans = {pid: name for pid, name in orphans.items()
               if not (name.lower().endswith("_condition")
                       and cityhash.package_id(name[:-len("_Condition")])
                       in packages)}
    if not orphans:
        return {}
    if not _utocs():
        say("      !! this pak retouches stock textures, but the game's own "
            "files were not found -- the retouch will not show in game")
        return {}

    frontier = {pid for pid in imported if pid not in packages}
    place = _locate(frontier | set(orphans))
    # A /Game/ package absent from the game overrides nothing -- author
    # leftovers. Renaming it along with everything else stays correct.
    real = {pid for pid in orphans if place.get(pid, {}).get("pkg")}
    if not real:
        return {}

    # Walk stock dependencies outward from what the pak imports until the
    # samplers of every retouched texture are found. Header data only; a
    # package is decompressed just when it is actually being carried.
    parent, seen = {}, set(packages)
    hits, entries = [], {}
    for _depth in range(3):
        nxt = set()
        for pid in sorted(frontier):
            if pid in seen:
                continue
            seen.add(pid)
            pl = place.get(pid)
            if not pl or not pl["pkg"]:
                continue
            ent = _entry(pid, pl)
            if ent is None:
                continue
            entries[pid] = (pl, ent)
            deps = set(ent[2])
            if deps & real:
                hits.append(pid)
            else:
                for d in deps:
                    if d not in seen and d not in parent and d not in orphans:
                        parent[d] = pid
                        nxt.add(d)
        found = set()
        for h in hits:
            found |= set(entries[h][1][2]) & real
        frontier = nxt
        if found == real or not frontier:
            break
        place.update(_locate(frontier))

    # A hit reached through intermediaries needs the whole chain carried, or
    # the renamed sampler is still only reachable via stock packages that
    # know nothing of it.
    graft_pids = set()
    for h in hits:
        p = h
        while p is not None and p not in graft_pids:
            graft_pids.add(p)
            p = parent.get(p)

    grafts, consumed = {}, set()
    for pid in sorted(graft_pids):
        pl, (exp, bun, deps) = entries[pid]
        u, k = pl["pkg"]
        data = _toc(u).read(k)
        bulks = []
        for bu, bk in pl["bulks"]:
            bt = _toc(bu)
            bulks.append((bytes(bt.chunk_ids[bk]), bt.read(bk)))
        grafts[pid] = dict(name=pkgedit.package_name_of(ZenPackage(data)),
                           data=data, exp=exp, bun=bun, deps=list(deps),
                           bulks=bulks)
        consumed |= set(deps) & real

    if grafts:
        tex = ", ".join(sorted(orphans[p].rsplit("/", 1)[-1]
                               for p in consumed))
        mats = ", ".join(sorted(g["name"].rsplit("/", 1)[-1]
                                for g in grafts.values()))
        say(f"      stock-texture retouch kept: {tex}")
        say(f"        (carrying {mats} from the game so it rides the outfit)")
    for p in sorted(real - consumed):
        say(f"      note: {orphans[p].rsplit('/', 1)[-1]} overrides a stock "
            "file nothing on this outfit uses (another costume slot or a "
            "weapon, say) -- that part won't apply in Dresscode form")
    return grafts


def rewrite(grafts, packages, renames, extra_imports):
    """Each grafted package's bytes, renamed with the same maps the pak's own
    rewrite used -- so its texture imports land on the pak's renamed copies."""
    pkgid_map, import_map, string_map = rename.build_maps(
        packages, renames, None, extra_imports)
    out = {}
    for pid, g in grafts.items():
        pkg = ZenPackage(g["data"])
        names = [rename.map_path(n, renames, string_map) for n in pkg.names]
        source = pkgedit.source_name_of(pkg)
        out[pid] = pkgedit.rewrite(
            g["data"], names=names, import_map=import_map,
            pkgid_map=pkgid_map,
            new_package_name=renames[g["name"].lower()],
            new_source_name=renames.get(source.lower(), source))
    return out
