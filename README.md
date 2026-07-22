# FFVII Rebirth Mesh Patcher

Repairs Final Fantasy VII Rebirth character mods broken by patch **V1.005**,
without downgrading the game.

V1.005 changed how character models (skeletal meshes) are stored, breaking any
mod built against the old layout. The symptom depends on the mod:

- **Dresscode** — the game fatal-crashes on startup; you never reach the menu.
- **A Dresscode costume** — Dresscode loads, but hovering that costume crashes.
- **A standalone pak mod** that replaces a character directly — it may or may not
  crash, but its textures and shading come out wrong.

Same underlying cause, so the same fix: this rewrites the affected models into
the format the current game expects. One command handles all of them — Dresscode,
costume mods, and loose pak mods, anything containing a skeletal mesh. It is not
a general-purpose mod fixer.

---

## Read this first

**This tool contains no mods.** It patches files you already have. You install
the mods yourself — Dresscode, costumes, or standalone pak mods — from wherever
their authors publish them.

**It is not affiliated with any mod it patches, or their authors.** It is an
independent fix, written by reverse-engineering the game's own file format.
Please do not raise problems with this tool anywhere except its own issue
tracker — the mod authors did not write it and cannot help with it.

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

- **Python 3.9 or newer** — from <https://www.python.org/downloads/>. In the
  installer, **tick "Add Python to PATH"**; skipping that box is the usual cause
  of a later `'python' is not recognized`.
- **NumPy** — a small library the patcher needs. After extracting the tool
  (below), double-click **`run.bat`** — it just opens a terminal in the tool's
  folder (nothing else; a few readable lines) — then type `pip install numpy`
  and press Enter. If that reports `'pip' is not recognized`, use
  `python -m pip install numpy` instead. You only do this once.

That's it. There is normally nothing else to configure.

---

## Setup

Everything runs from a terminal inside the extracted folder — the same one from
Requirements: double-click **`run.bat`**, or type `cmd` in File Explorer's
address bar while in the folder. Then run:

```
python patch.py --list
```

It finds the game and everything else on its own:

```
  Game   (detected):  C:\Program Files (x86)\Steam\steamapps\common\FINAL FANTASY VII REBIRTH
  Oodle  (detected):  C:\Program Files (x86)\Steam\steamapps\common\SomeGame\oo2core_9_win64.dll
```

**The game** is found either by Steam's library list, or by noticing the tool is
sitting inside the game folder — so dropping it anywhere under the install works
too, whether that's the base game folder or `End\Mods\`.

**The Oodle library** decompresses mod archives. FFVII Rebirth builds Oodle into
its executable, so there is no copy in the game folder to borrow. It is
proprietary and cannot be bundled here — but it ships as a loose
`oo2core_*_win64.dll` with a number of games. You need **oo2core_6 or newer**;
oo2core_5 and older can't decode this game. The tool looks beside itself first,
then through your installed Steam, Epic and GOG games, and any Unreal Engine
install.

Only a minority of games include it — roughly one in twenty — but they tend to
be large titles, so there's a fair chance you already have one. Games known to
ship a working copy:

- **ELDEN RING** — `Game\oo2core_6_win64.dll`
- **DOOM Eternal** — `oo2core_8_win64.dll` (in the game root)
- **DEATH STRANDING DIRECTOR'S CUT** — `oo2core_7_win64.dll` (in the game root)
- **Indiana Jones and the Great Circle** — `oo2core_9_win64.dll` (in the game root)

If you have none of those, **Unreal Engine ships one** and is free from the Epic
Games Launcher. Install it and the tool finds the DLL on its own. If it can't,
search the engine folder for **`oo2core.dll`** (recent versions) or
**`oo2core_*_win64.dll`** (older ones) and drop it next to `patch.py` — take the
copy under a **`win-x64`** folder, never `win-x86`. A large download for one
file, but it always works.

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
python patch.py ModName     patch one mod, by its folder or .utoc name
```

It scans two places: `End\Mods\` (the FF7RML mod loader) and
`End\Content\Paks\~mods\` (loose pak mods the game loads directly). Mods in the
first are named by their folder; mods in the second by their `.utoc` filename,
shown with a `(~mods)` tag.

Example:

```
  Game   (detected):  C:\...\steamapps\common\FINAL FANTASY VII REBIRTH
  Oodle  (detected):  C:\...\steamapps\common\SomeGame\oo2core_9_win64.dll
  Mods   :            C:\...\FINAL FANTASY VII REBIRTH\End\Mods

  Dresscode  (the base mod, by YIISx)
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

Only mod files are ever modified, never the game's own packages. Loose pak mods
live under `End\Content\Paks\~mods\`, so those files sit inside the game folder —
but the game's own `.pak`/`.utoc`/`.ucas` (in `Paks\` itself) are never touched.

### Other options

```
--debug        add per-mesh detail to --list
--pause        wait for a keypress before closing
--no-pause     never wait
```

---

## Troubleshooting

**`Could not find an Oodle library`**
Search your game folders for `oo2core_*_win64.dll` — or, inside an Unreal Engine
install, the unversioned `oo2core.dll` (take the one under a `win-x64` folder,
never `win-x86`) — and copy it next to `patch.py`. See the Setup section for
games known to ship one.

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
