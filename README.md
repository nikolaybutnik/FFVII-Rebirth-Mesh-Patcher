# FFVII Rebirth Mesh Patcher

Repairs Final Fantasy VII Rebirth character mods broken by patch **V1.005**,
without downgrading the game.

V1.005 changed how character models (skeletal meshes) are stored, breaking any
mod built against the old layout. The symptom depends on the mod:

- **A Dresscode costume** — Dresscode loads, but hovering that costume crashes.
- **A standalone pak mod** that replaces a character directly — it may or may not
  crash, but its textures and shading come out wrong.

Same underlying cause, so the same fix: this rewrites the affected models into
the format the current game expects. One command handles them all — costume mods
and pak mods, anything containing a skeletal mesh. It is not a
general-purpose mod fixer.

**Dresscode itself now has an official V1.005 update** from its author, so this
tool no longer patches Dresscode — install that release directly if the menu
crashes. This patcher is for the costume and character mods that still need it.

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
match what the current game expects. If a mod author releases a version already
built for V1.005 or later (as Dresscode's author now has), that version will not
need patching — and if the file layout changes, this tool may need updating
before it works again. It refuses rather than guessing when it meets something it
does not recognise, so a mismatch should show up as "could not be read", not as a
broken install.

**If an official fix exists, use that instead.** This exists because one had not
appeared. If that changes, the author's own release is the better option.

---

## Requirements

- **Python 3.9 or newer** — from <https://www.python.org/downloads/>. In the
  installer, **tick "Add Python to PATH"**; skipping that box is the usual cause
  of a later `'python' is not recognized`.
- **NumPy** — a small library the patcher needs. After extracting the tool
  (below), open a terminal in its folder — type `cmd` in File Explorer's address
  bar while in the folder, or right-click the folder and choose "Open in
  Terminal" — then type `pip install numpy` and press Enter. If that reports
  `'pip' is not recognized`, use `python -m pip install numpy` instead. You only
  do this once.

That's it. There is normally nothing else to configure.

---

## Setup

Everything runs from a terminal inside the extracted folder — the same one from
Requirements: type `cmd` in File Explorer's address bar while in the folder (or
right-click the folder and choose "Open in Terminal"). Then run:

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

Also reported by users to ship a working copy, but I don't own these so I can't
confirm the exact filename or location — search the game folder for `oo2core`:

- **Warhammer 40,000: Darktide**
- **ELDEN RING NIGHTREIGN**

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
`End\Content\Paks\~mods\` (pak mods the game loads directly). Mods in the
first are named by their folder; mods in the second by their `.utoc` filename,
shown with a `(~mods)` tag.

Example:

```
  Game   (detected):  C:\...\steamapps\common\FINAL FANTASY VII REBIRTH
  Oodle  (detected):  C:\...\steamapps\common\SomeGame\oo2core_9_win64.dll
  Mods   :            C:\...\FINAL FANTASY VII REBIRTH\End\Mods

  Dresscode  (the base mod, by YIISx)
    [ok]  installed -- not patched by this tool
          If Dresscode itself crashes, get the author's official
          V1.005 release.

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

Only mod files are ever modified, never the game's own packages. Pak mods
live under `End\Content\Paks\~mods\`, so those files sit inside the game folder —
but the game's own `.pak`/`.utoc`/`.ucas` (in `Paks\` itself) are never touched.

### Mods that aren't installed

The tool also works on any folder of mods. This needs only the Oodle DLL, not
the game, so it runs on a machine without FFVII Rebirth installed:

```
python patch.py --path "D:\my mods"                  list that folder
python patch.py --path "D:\my mods" --all            patch it in place
python patch.py --path "D:\my mods" --out "D:\send"  patched COPIES to --out,
                                                     originals left untouched
```

In-place patches back up to a `_patch_backups\` folder inside the folder itself;
`python patch.py --restore --all --path "D:\my mods"` puts them back.

Or skip the command line: **drag mod folders — or `.zip`/`.7z`/`.rar` archives —
onto `patch.py`**. It lists what it found, then offers to patch everything into a
`Patched Mods` folder beside the original; your originals are never touched. Drop
as many as you like at once, and each may hold several mods, or archives inside
archives — all of it gets unpacked. `.7z` and `.rar` need nothing installed.

Two naming rules are handled for you. The `.utoc`/`.ucas`/`.pak` files keep their
exact names, because the loader would lose track of a renamed mod. And a Dresscode
mod's folder is renamed to match the `.uplugin` inside it — Dresscode looks a mod
up by folder name and silently ignores one that doesn't match, which is how some
downloads arrive. Correctly packaged mods are left alone.

Dropping folders from inside the game's own `End\Mods` or `~mods` is recognized as
your installed library: you get the normal in-place patch, with backups in
`backups\`, rather than a copy.

### Other options

```
--debug        add per-mesh detail to --list
--pause        wait for a keypress before closing
--no-pause     never wait
```

### Going the other way (unpatch.py)

Still on game version 1.004, or rolled back to it? `unpatch.py` converts mods
**back** to the pre-V1.005 format. It is `patch.py` in reverse and works
exactly the same way — drag a mod folder or archive onto it and you get an
`Unpatched Mods` copy beside the original, or use the same flags:

```
python unpatch.py --list              show every mod and whether it needs it
python unpatch.py --all               unpatch everything that needs it
python unpatch.py --path "D:\mods" --out "D:\send"   copies, originals kept
```

In-place runs back up to `unpatch_backups\` (folder drops to
`_unpatch_backups\` inside the folder), and `--restore` undoes from there.
Patching and unpatching are mutual inverses: a mod taken down to 1.004 and
back comes out identical, apart from tangent rounding far below anything
visible.

---

## Converting between formats (convert.py)

`convert.py` turns a costume mod from one format into the other, in either
direction: a **Dresscode** mod (picked in the Dresscode menu) into **loose
paks** (dropped in `~mods`, always worn), or the other way around. Drop a mod
folder or archive onto `convert.py`, or run:

```
python convert.py "D:\mods\Some Mod"
```

Originals are never touched; everything new is written beside them.

### Dresscode → paks

Nothing to organize — point it at the mod folder (the one holding the
`.uplugin`) and every outfit becomes its own pak, menu variants become
`Optional` paks, and a `dresscode.json` is written beside them. That file
remembers the original, so converting the folder back later rebuilds it
exactly. **Leave `dresscode.json` where it is** if you ever want the round
trip.

### Paks → Dresscode: how to organize the folder

Put ONE mod in one folder and drop that folder. First drop writes
`dresscode.json` (open it to set names, or don't); second drop builds.

```
My Mod\
├── dresscode.json          written by the first drop
├── icon.png                optional -- the mod's thumbnail
├── SomeOutfit_P.utoc/.ucas/.pak     the outfit (anywhere: root or any
│                                    subfolder, however deep)
├── SomeTextures_P.*        a pak the outfit NEEDS (its textures or
│                           materials in a separate download) -- put it
│                           beside the outfit, it is detected and merged
├── Variants\
│   ├── No Jacket\SomeOutfit_P.*     a WHOLE costume of its own -- one
│   └── Short Hair\SomeOutfit_P.*    extra outfit tile in the same mod
└── Optional\
    ├── No Hat\No_Hat_P.*            add-on paks, listed for you to
    └── Red\Red_Recolor_P.*          combine -- see below
```

The rules, in plain terms:

- **Outfit paks** (the mod's mains) can sit anywhere — the root, `Main\`,
  or nested download folders. Several outfits are fine; each becomes its
  own Dresscode mod, named "mod - outfit".
- **Option paks** — the little hide-this / recolor-that files — are
  understood wherever they are. Under `Optional\` they always count as
  options; anywhere else the converter reads their contents and works out
  whether they are options (they change the outfit's look) or **required
  companions** (the outfit's own materials point into them — those merge
  into every outfit automatically, and the output says so).
- **Whole alternate costumes** — a version with a part left out, a
  different body, a different hairstyle — go under `Variants\`, one
  folder each. They become extra **outfit tiles inside the same Dresscode
  mod**, named after their folder, each able to have its own
  `preview.png`. Alternates sitting loose beside the mains instead still
  become separate Dresscode mods, which is the older behaviour.
- **`Optional\` is a different thing, from the modular PAK standard.**
  Those are the little add-on paks you drag in beside a pak mod — a
  recolour, a piece hidden by making its material invisible. They change
  MATERIALS, so a difference that lives in the model itself (a part
  switched off, a different body) has no add-on form and would do nothing.
- **Add-ons are listed, not turned into menu entries.** Dresscode has one
  menu, and picking an entry replaces the last one — so a mod with 30
  add-ons would become 30 entries that each change one thing, which is not
  how anyone wears them. Instead `dresscode.json` lists the parts under
  `parts_you_can_combine` and leaves `variants` empty. Write the
  combinations you actually want:

  ```json
  "variants": [
    { "name": "Red, no hat", "parts": ["Red_Recolor_P", "No_Hat_P"] },
    { "name": "Just red",    "parts": ["Red_Recolor_P"] }
  ]
  ```

  Anything you want to wear together has to be ONE entry.

  Don't want to choose? Set `"stackable": true` instead. The parts stay
  drop-in files: the build gives you a "Put in ~mods" folder, you drop in
  the ones you want and mix them exactly as you do today, and only the
  costume itself is picked in Dresscode.
- **A mod uses one shape or the other.** A folder holding both `Variants\`
  and `Optional\` is refused, and the message says which paks it found
  and what each shape is for. Most Dresscode mods want `Variants\`;
  `Optional\` is there for modular pak mods, and for a mod converted
  out of Dresscode — whose own menu entries land there and ARE kept, so
  converting it back reproduces the mod exactly.
- **Another Dresscode mod this one depends on** (a shared asset mod
  its page says to install): put that mod's folder in the same parent folder
  as the one you drop, or have it installed in `End\Mods`. The converter
  says whether it found it — and if it did not, the conversion still
  works, minus whatever the missing mod provides (same as in game).
- **Pictures**: a picture next to `dresscode.json` is the mod's thumbnail;
  a picture inside an outfit's folder is that outfit's preview. Name them
  `icon.png` / `preview.png` if a folder has several.
- Weapon paks and other non-costume files riding in the same download are
  skipped with a note — they have no Dresscode form.
- **Retouched game textures**: some mods also ship corrected copies of the
  game's own textures (skin shading, say) so the outfit blends cleanly.
  The converter keeps those working on its own — the output says
  "stock-texture retouch kept" when it happens. Parts of a mod that
  override something the outfit itself never uses (another costume slot,
  a weapon skin) have no Dresscode form, and the output says that too.

Pre-V1.005 mods are caught automatically: if the meshes are the old
format, `convert.py` says so before doing anything and offers to patch
them right there (backups kept). Say yes and the conversion continues
with the fixed files.

### Combine options into your own variants

A fresh conversion leaves `"variants"` empty and lists every option pak
under `"parts_you_can_combine"` — option paks do NOT become tiles on
their own. That's deliberate: **tiles don't stack in game**. Pick "Red"
and then "No Hat" and you'd get a hatless outfit in the normal colour,
because the second tile replaces the first — a tile per option would
just be clutter that works wrong.

Making tiles is a copy-paste job — no tools, just Notepad. Open
`dresscode.json`, find the `"variants"` list, and give each look you
actually wear one entry:

```json
"variants": [
  { "name": "No Hat", "parts": ["No_Hat_P"] },
  { "name": "Red",    "parts": ["Red_Recolor_P"] }
]
```

One entry = one tile. `"name"` is what the tile says in the menu,
`"parts"` are the option paks it applies (their file names). So to get a
red AND hatless tile, copy an entry and list both paks:

```json
"variants": [
  { "name": "No Hat",      "parts": ["No_Hat_P"] },
  { "name": "Red",         "parts": ["Red_Recolor_P"] },
  { "name": "Red, no hat", "parts": ["Red_Recolor_P", "No_Hat_P"] }
]
```

Save, drop the folder on `convert.py` again, done — "Red, no hat" is now
its own tile. Some notes:

- Combine as many parts in one entry as you want.
- If two parts change the same thing, the one listed later wins.
- The menu shows exactly this list: rename tiles, delete the ones you
  never use, or replace the whole list with a few favourite combos.
- Made a mess of the file? Delete `dresscode.json` and drop the folder
  again for a fresh one. (Only do that if the file has no `"restore"`
  section — that section is the mod's way back to its original form.)

---

## Leaving parts out (devtools\parts.py)

A workbench tool, separate from patching. Drag a pak mod — folder, `.utoc`
or `.zip` — onto `devtools\parts.py` and it takes the model apart into the pieces it is
actually made of:

```
   #  part                        triangles   material
   1  <part name>                    28,128   <material>
   2  <part name>                    19,814   <material>
   ...
  12  <part name>                     3,568   <shared material>
  13  <part name>                     5,820   <shared material>
  (<shared material> is shared by parts 7, 10, 11, 12, 13)
```

The names are the mod author's own, and usually say what each piece is. Answer
with the parts to leave out — `12 13`, part of a name, or a range like `5-7` —
and it writes a fresh copy of the mod with those switched off, into `parts out\`
in the patcher folder. Your original is never touched.

```
python devtools\parts.py "D:\mods\MyMod"              list, then ask
python devtools\parts.py "D:\mods\MyMod" --list       just list
python devtools\parts.py "D:\mods\MyMod" --omit 12,13 no questions
python devtools\parts.py "D:\mods\MyMod" --omit none  put every part back
```

Worth knowing:

- **Nothing is deleted.** Each part is switched off with the flag the engine
  itself uses for "do not draw this section", so the geometry stays in the file
  and turning a part back on is the same edit in reverse.
- **Your answer is the whole list, not an addition.** Run it again with a
  different answer and that becomes the omitted set, so there is no way to paint
  yourself into a corner.
- Several parts often **share one material** — the line under the table says
  which. That is why hiding by material takes five things off at once and this
  takes exactly one.
- If the mod still needs the V1.005 fix, it says so; run `patch.py` on
  the result as well, in either order.
- Output goes to `parts out\` rather than next to the mod on purpose: the game
  loads `~mods` recursively, so a copy left beside its original would be loaded
  alongside it.

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

**Patched, but the model still looks wrong (grey checkerboard skin)**
Most likely not the mesh. Many costume mods keep their skin textures in a
separate companion mod; without it the costume loads but renders grey/checkered,
and no mesh fix can correct that. `--list` flags known cases under **Missing
required files** — otherwise check the mod page's Requirements section.

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

`unpatch.py` reverses all three: the duplicated-vertex arrays return (in the
empty form real 1.004 mods carry), tangents expand back to the 8-byte
standard, and half-float UVs stay — they were always legal.

---

## Licence

MIT — see [LICENSE](LICENSE). Use it, change it, ship it, fold it into your own
tool. Attribution is appreciated but not required.

The licence covers this tool only. It does not cover any mod you use it on, and
no mod content is included or redistributed here.
