# FF7 Rebirth Mesh Patcher

Repairs Final Fantasy VII Rebirth mods broken by game patch **V1.005**, without
downgrading the game.

V1.005 changed how character models are stored. Any mod containing a custom
character model breaks — in Dresscode, hovering the costume crashes the game to
desktop. This rewrites those models into the new format, in place.

Works on **Dresscode itself** and on **costume mods**. Same command for both.

---

## Read this first

**This tool contains no mods.** It patches files you already have. You need to
find and install Dresscode and any costume mods yourself, from wherever their
authors publish them.

**It is not affiliated with Dresscode or its author.** It is an independent fix,
written by reverse-engineering the game's own file format. Please do not raise
problems with this tool anywhere except its own issue tracker — the mod authors
did not write it and cannot help with it.

**It may stop working when mods update.** This patcher rewrites mod files to
match what the current game expects. If the Dresscode author (or any mod author)
releases a version already built for V1.005 or later, that version will not need
patching — and if the file layout changes, this tool may need updating before it
works again. It refuses rather than guessing when it meets something it does not
recognise, so a mismatch should show up as "could not be read", not as a broken
install.

**If an official fix exists, use that instead.** This exists because one had not
appeared. If that changes, the author's own release is the better option.

---

## Requirements

- **Python 3.9 or newer** — <https://www.python.org/downloads/>
  (tick *"Add Python to PATH"* during install)
- **NumPy** — after installing Python, run: `pip install numpy`

That's it. There is normally nothing to configure.

---

## Setup

Extract it anywhere and run:

```
python patch.py --list
```

It finds the game and everything else on its own:

```
  Game   (detected):  C:\Program Files (x86)\Steam\steamapps\common\FINAL FANTASY VII REBIRTH
  Oodle  (detected):  C:\Program Files (x86)\Steam\steamapps\common\SomeGame\oo2core_9_win64.dll
```

**The game** is found either by Steam's library list, or by noticing the tool is
sitting inside the game folder — so dropping it in `End\Mods\` also works.

**The Oodle library** decompresses mod archives. FF7 Rebirth builds Oodle into
its executable, so there is no copy in the game folder to borrow. It is
proprietary and cannot be bundled here — but it ships as a loose
`oo2core_*_win64.dll` with a number of games, and **any version works**. The
tool looks beside itself first, then through your installed Steam, Epic and GOG
games, and any Unreal Engine install.

Only a minority of games include it — roughly one in twenty — but they tend to
be large titles, so there's a fair chance you already have one. Games known to
ship it:

- **ELDEN RING** — `Game\oo2core_6_win64.dll`
- **Grand Theft Auto V Enhanced**
- **DEATH STRANDING DIRECTOR'S CUT**
- **Indiana Jones and the Great Circle**

If you have none of those, **Unreal Engine ships one** and is free from the Epic
Games Launcher (`Engine\Binaries\ThirdParty\Oodle\...`). A large download for
one file, but it always works.

If the tool can't find one, it will ask:

```
  Drag the file onto this window and press Enter, or paste its path.
  >
```

Dragging the DLL onto the console window pastes its path. The tool copies it
next to `patch.py`, so you are only asked once. You can also just put the file
there yourself beforehand.

---

## Usage

Install your mods as normal first, then:

```
python patch.py --list      show every mod and whether it needs fixing
python patch.py --all       patch everything that needs it
python patch.py ModName     patch one mod, by its folder name
```

Example:

```
  Game   (detected):  C:\...\steamapps\common\FINAL FANTASY VII REBIRTH
  Oodle  (detected):  C:\...\steamapps\common\SomeGame\oo2core_9_win64.dll
  Mods   :            C:\...\FINAL FANTASY VII REBIRTH\End\Mods

  Dresscode  (the outfit menu itself)
    [ok]  patched           2 meshes

  Mods with character meshes
    [ok]  ExampleOutfit              patched         1 mesh
    [!!]  AnotherOutfit              needs patching  1 mesh

  No character meshes -- unaffected by V1.005
    SomeOtherMod

  1 mod needs patching:  AnotherOutfit
  Run:  python patch.py --all
```

| marker | meaning |
|---|---|
| `[ok]` | already in the new format — nothing to do |
| `[!!]` | still in the old format — this is what gets fixed |
| `[--]` | no character meshes — unaffected by V1.005 |
| `[??]` | could not be read — run with `--debug` |

### Undo

Every mod is backed up to `backups/<ModName>/` before anything is written.

```
python patch.py --restore --all       put everything back
python patch.py --restore ModName     put one mod back
```

Your game files are never modified — only files inside `End\Mods\`.

### Other options

```
--debug        add per-mesh detail to --list
--pause        wait for a keypress before closing
--no-pause     never wait
```

---

## Troubleshooting

**`Could not find an Oodle library`**
Search your game folders for `oo2core_*_win64.dll` and copy it next to
`patch.py`. See the Setup section for games known to ship one.

**`no skeletal meshes -- unaffected`**
That mod has no character model, so V1.005 didn't break it. Nothing to do.

**A mod reports multiple LODs and refuses**
This handles single-detail-level models, which covers every costume mod tested
so far. It refuses rather than guessing. Please report the mod name.

**Patched, but the model still looks wrong**
Most likely not the mesh. Check whether the mod page lists a required mod — a
missing material renders as flat grey or a patchwork, and no mesh fix can
correct that. Install the requirements first.

**The game still crashes**
Run `python patch.py --restore --all`, then report the problem including the
output of `python patch.py --list --debug`.

---

## What it actually changes

Three things, all inside the mod's own files:

1. Removes `FDuplicatedVerticesBuffer` from every render section — V1.005
   dropped it, and mods that still write it desync the loader and crash.
2. Converts the per-vertex tangent frame to the new 4-byte encoding
   (from either the 8-byte standard or the 16-byte high-precision form).
3. Converts full-precision texture coordinates to half floats, when a mod uses
   them, because the current shaders read them as half.

Converted models match the artist's original data to within **0.1 degrees**.

---

## Licence

MIT — see [LICENSE](LICENSE). Use it, change it, ship it, fold it into your own
tool. Attribution is appreciated but not required.

The licence covers this tool only. It does not cover any mod you use it on, and
no mod content is included or redistributed here.
