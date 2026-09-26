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
crashes. This patcher is for the costume and weapon mods that still need it.

---

## Quick start

1. **Install Python 3.9 or newer** from <https://www.python.org/downloads/>,
   ticking **"Add python.exe to PATH"** if the installer offers it. If
   `python` isn't recognised afterwards, try `py` instead — see
   [Troubleshooting](#troubleshooting).
2. **Extract this tool**, open a terminal in its folder (type `cmd` in File
   Explorer's address bar), and run `pip install numpy`. Once, ever.
3. **Drag your mod folder — or its `.zip` — onto `patch.py`.** It lists what it
   found and offers to fix it, writing patched copies into a `Patched Mods`
   folder beside the original. Your files are not touched.

Mods already installed in the game? Run `python patch.py --all` instead: it
finds them and patches in place, keeping backups.

The first run may ask you for an Oodle DLL — [Setup](#setup) explains where to
get one.

---

## Read this first

**This tool contains no mods.** It patches files you already have, and is not
affiliated with any mod or its author. Please raise problems with this tool on
its own issue tracker only — the mod authors did not write it and cannot help.

**It may stop working when mods update.** A version already built for V1.005 or
later needs no patching, and a future file-layout change may need this tool
updated first. It refuses rather than guesses when it meets something it does
not recognise, so a mismatch shows up as "could not be read", not as a broken
install.

---

## Setup

There is normally nothing to configure. Run this in the tool's folder:

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

**The Oodle library** decompresses mod files. Rebirth builds it into its
executable and it can't be bundled here, but it ships as a loose
`oo2core_*_win64.dll` with a number of games — you need **oo2core_6 or newer**.
The tool searches beside itself, then your Steam, Epic and GOG games and any
Unreal Engine install.

Games known to ship one (search the folder for `oo2core` — the name and
location vary):

- **Final Fantasy VII Remake**
- **Armored Core VI: Fires of Rubicon**
- **DOOM Eternal**
- **Death Stranding Director's Cut**
- **ELDEN RING**
- **ELDEN RING NIGHTREIGN**
- **Indiana Jones and the Great Circle**
- **Need for Speed Heat**
- **SMITE**
- **Star Wars Jedi: Survivor**
- **Warhammer 40,000: Darktide**

If you have none of those, **Unreal Engine ships one** and is free from the
Epic Games Launcher — a large download for one file, but it always works. Take
the copy under a **`win-x64`** folder, never `win-x86`.

When the tool can't find one it asks:

```
  Drag the file onto this window and press Enter, or paste its path.
  >
```

Dragging the DLL onto the window pastes its path, and it is copied next to
`patch.py` so you are asked only once.

---

## Linux

In order to run on Linux you need [linoodle](https://github.com/McSimp/linoodle),
which is fetched at build time. Needs `cmake`, `g++`, and `git`.
Build once:

```
cmake -S third_party -B third_party/build -DCMAKE_BUILD_TYPE=Release
cmake --build third_party/build --target linoodle
```

**Or skip the build.** Unreal Engine on Linux ships a native
`liboo2corelinux64.so.9`. Rename it to `oo2core_9_win64.dll`, put it next to
`patch.py`, and it loads directly -- no linoodle, no cmake. Credit to riffews
on Nexus Mods for the workaround.

Only Steam is found automatically on Linux. For anything else, point
`OODLE_DLL` in `config.py` at the DLL yourself.

---

## Usage

### Drag and drop

**Drag mod folders — or `.zip`/`.7z`/`.rar` archives — onto `patch.py`.** No
terminal, no flags. It lists what it found, then patches into a `Patched Mods`
folder beside the original; your originals are never touched. Drop as many as
you like at once — nested archives and multi-mod folders are unpacked, and
`.7z`/`.rar` need nothing installed.

Naming is handled for you: the three mod files keep their exact names, and a
Dresscode mod's folder is renamed to match its `.uplugin` when a download
arrives mismatched (Dresscode ignores a mod whose folder doesn't match).

Drop folders from inside the game's own `End\Mods` or `~mods` and it recognises
your installed library: you get the normal in-place patch with backups, not a
copy.

### From the command line

Install your mods as normal first, then:

```
python patch.py --list      show every mod and whether it needs fixing
python patch.py --all       patch everything that needs it
python patch.py ModName     patch one mod, by its folder or .utoc name
```

It scans two places: `End\Mods\` (mods installed through the FF7RML mod
loader, which is what Dresscode uses) and `End\Content\Paks\~mods\` (pak mods
the game loads directly). Mods in the first are named by their folder; mods in
the second by their `.utoc` filename, shown with a `(~mods)` tag.

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

Only mod files are ever modified. Pak mods sit inside the game folder, under
`End\Content\Paks\~mods\`, but the game's own files in `Paks\` are never
touched.

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

### Other options

```
--help         short command list
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

**Every `patch.py` command works on `unpatch.py` unchanged.** The only
difference is that backups go to `unpatch_backups\` (or `_unpatch_backups\`
for folder drops), and each tool's `--restore` undoes its own work.

---

## Converting between formats (convert.py)

`convert.py` turns a costume mod from one format into the other, in either
direction: a **Dresscode** mod (picked in the Dresscode menu) into **loose
paks** (dropped in `~mods`, always worn), or the other way around.

Throughout this section, a **tile** means one entry in the Dresscode menu — the
thing you click to wear something.

Drop a mod folder or archive onto `convert.py`, or run:

```
python convert.py "D:\mods\Some Mod"
```

Originals are never touched; everything new is written beside them.

### Dresscode → paks

Nothing to organize — point it at the mod folder (the one holding the
`.uplugin`) and every outfit becomes its own pak, menu variants become
`Optional` paks, and a `dresscode.json` and `dresscode.bin` are written
beside them. Weapons-menu rows convert too, one pak each. Those two remember
the original, so converting the folder back later rebuilds it exactly. **Leave
both where they are** if you ever want the round trip.

### Paks → Dresscode: how to organize the folder

Put ONE mod in one folder and drop that folder. First drop writes
`dresscode.json` (open it to set names, or don't); second drop builds.

**Set it up like this.** The converter accepts most shapes; this one is the
easiest to get right.

```
My Mod\
├── icon.png                    the mod's thumbnail           (optional)
├── Variants\                   every costume, one folder each
│   ├── Standard\
│   │   ├── Whatever_P.utoc     the costume's three files, loose in the
│   │   ├── Whatever_P.ucas       folder -- plus any pak it needs, like
│   │   ├── Whatever_P.pak        a separate textures one
│   │   └── preview.png         this tile's picture           (optional)
│   ├── No Jacket\
│   └── Short Hair\
└── Optional\                   only if the mod has add-ons
    ├── Red\                    ONE copy of each add-on, never one
    └── No Hat\                   copy per costume
```

That gives you a single Dresscode entry with a tile per costume. Use it even
for a one-costume mod — `Variants\Standard\` on its own is fine.

Habits that save trouble:

- **Name the folders the way you want the tiles named** — those names go
  straight into the menu. Keep them short; very long ones get shortened.
- **Unpack down to the three files** (`.utoc`, `.ucas`, `.pak`). Downloads
  often arrive wrapped in `~mods\` or `Content\Paks\WindowsNoEditor\`.
- **Nothing loose at the top** but `icon.png`, `dresscode.json` and
  `dresscode.bin`.
- **None of it is required.** Paks are found however deep they sit, and those
  wrappers are ignored when naming tiles — an untouched download converts
  fine.

**Whole costume, or a change to one?** A whole costume replaces the model — a
different body, a hairstyle, a part modelled away — and goes under
`Variants\`. A change only repaints or hides what is already there (a recolour,
a hidden buckle) and goes under `Optional\`. Get it wrong and the converter
says so.

| What you have | Where it goes | What you get |
| --- | --- | --- |
| every whole costume | `Variants\<name>\` | one tile each, all in one Dresscode mod |
| changes to a costume | `Optional\<name>\` | listed for you to combine — see below |
| a pak the costume NEEDS (its textures or materials, shipped separately) | beside that costume | merged in automatically |
| whole costumes you want as **separate mods** instead | any other subfolder | a Dresscode mod per costume |
| a weapon mod, with no costume in it | the weapon paks, loose in the folder | one WEAPONS-menu tile each |
| a recolour, with no model of its own | each colour in its own folder | a tile per colour, on the costume they were made for — needs the game installed |

Pre-V1.005 mods are caught for you: `convert.py` says so before doing anything
and offers to patch them right there, backups kept.

### Making your own tiles

Option paks don't become tiles on their own, because **tiles don't stack in
game** — pick "Red" and then "No Hat" and you get a hatless outfit in the
normal colour, as the second tile replaces the first. So you say which
combinations you want. (Weapon paks are the exception: their tiles stand
alone, so those entries are written for you.)

Open `dresscode.json`, find the `"variants"` list, and give each look one
entry:

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

- Combine as many parts in one entry as you like. If two change the same
  thing, the one listed later wins.
- The menu shows exactly this list — rename tiles, or delete ones you never use.
- `"description"` on an entry adds a second line under that tile, the same as
  an outfit's. Left out, the tile shows its name alone.
- An entry goes on every outfit. Add `"outfit": "Standard"` (or a list of
  names) to limit it to some. Costume add-ons only; it does nothing on a weapon.
- Made a mess? Delete `dresscode.json` and drop the folder again — unless it
  has a `"restore"` section, which is the mod's way back to its original form
  (`dresscode.bin` holds the rest of it).

### Details

Everything below is reference — you don't need any of it for an ordinary mod.

- **Add-ons apply to every costume**, so three costumes and two add-ons give
  three tiles plus their combinations, in one Dresscode entry. To put one on
  only some costumes, add `"outfit"` to its entry:

  ```json
  "variants": [
    { "name": "Red",       "parts": ["Red_Recolor_P"] },
    { "name": "No hat",    "parts": ["No_Hat_P"], "outfit": "Standard" },
    { "name": "No jacket", "parts": ["No_Jacket_P"],
      "outfit": ["Standard", "Short Hair"] }
  ]
  ```

  Name an outfit as it appears in the `"outfits"` list — its `name` or its
  folder. A name matching nothing stops the conversion and lists the real ones.
- **Don't want to compose tiles at all?** Set `"stackable": true` and the parts
  stay drop-in files: you get a "Put in ~mods" folder that works as always,
  with only the costume in the menu.
- **Weapon paks become weapon tiles, and the WEAPONS menu is its own.** A
  weapon tile belongs to a character, not a costume, so it stays on whatever
  they are wearing. A weapon mod needs no costume: drop the paks on their own,
  `"outfits"` stays empty, and the menu is written for you — one tile per
  weapon even when a single pak covers a character's whole set, and paks that
  draw on each other written into one entry, so a weapon split across two paks
  comes out whole.
- **Only weapons the game equips get a tile.** The data also holds cutscene
  variants and the first game's weapons; a tile each would bury the real one.
- **A weapon that ships its own model** converts with nothing installed. One
  that only recolours the stock model borrows it from the game, so that pak
  needs the game and is otherwise skipped with a note — it keeps working from
  `~mods`. Coming back, a tile this tool made returns to an override pak by
  itself; a weapon mod someone else wrote for Dresscode needs the game (or
  Dresscode) installed to find which weapon it stands in for, and one whose
  files carry no id is skipped rather than guessed at.
- **A separate textures pak is detected, not configured.** If the costume needs
  it, it is merged in; one sitting in a costume's folder belongs to that
  costume alone, so mods shipping several versions keep their own.
- **Retouched game textures** — many older mods repaint the game's own textures
  (skin shading, say) instead of shipping their own. Those ride with the
  costume, so they apply while it is worn and nowhere else. A part overriding
  something the outfit never uses has no Dresscode form, and the output says so.
- **Another Dresscode mod this one depends on**: put its folder in the same
  parent folder as the one you drop, or have it installed in `End\Mods`.
  Without it the conversion still works, minus whatever that mod provides —
  same as in game.
- **Pictures**: one next to `dresscode.json` is the mod's thumbnail; one inside
  an outfit's folder is that outfit's preview. Name them `icon.png` /
  `preview.png` if a folder holds several. An **add-on pak** — a costume
  toggle or a weapon tile — takes a `.png` named after the pak, beside the
  pak (`Recolor_P.utoc` → `Recolor_P.png`); paks share a folder, so the name
  is what says which is which. Without one a weapon tile shows the game's
  picture of the weapon it replaces, and a toggle shows the plain
  placeholder.
- **What gets refused**: several different costume paks loose in the top folder
  with nothing saying which is which — give each its own subfolder, or one
  shared `Main\`. Also a folder with no costume pak in it, unless what's in
  there is weapon paks or recolours.

---

## Aiming a pak at another costume or weapon (repoint.py)

A `~mods` pak replaces one particular costume — whichever one the author built
it on. `repoint.py` moves it to a different one.

Drag a pak mod's folder onto `repoint.py`, or run:

```
python repoint.py "D:\mods\Some Pak Mod"
```

It says what each pak replaces now, lists that character's other costumes (or
weapons), and asks which you want instead. Answer with numbers — `3`, `2,5,7`,
`2-5`, or `all`. Repointed copies go into a `(Repointed)` folder beside the original, which is never touched.

- **Paks only.** A Dresscode mod already lets you pick in its menu. Run
  `convert.py` on one first if you want it as paks.
- **Same character only.** Another character's costume is built on another
  skeleton, so it isn't offered.
- **Picking several gives you one pak, not several.** The mesh is copied onto
  each costume and everything else stays in one place, so three costumes cost
  barely more than one.
- **Drop the whole mod folder**, not one pak at a time. A mod's optional
  extras override files inside the costume's folder; move the main pak alone
  and they are left aiming at files nothing uses. Dropping the folder moves
  them together.
- **It tells you what won't carry over.** A mod may replace one of the game's
  own files — a skin texture, say — that only exists on its original costume;
  that replacement stops applying. The outfit still works, and the tool lists
  what dropped out. This is the one part that needs the game installed.
- **No game install needed** otherwise. The list of costumes and weapons is
  the same on every copy of the game, so it ships with the tool — you only
  need the Oodle DLL, same as `patch.py --path`.

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

**`'python' is not recognized`**
Either Python isn't on your PATH, or you installed it with the **Python install
manager** — which sets up `py` rather than `python`. Try **`py patch.py --list`**
first: if that works, use `py` in place of `python` everywhere here. Otherwise
reinstall Python and tick the PATH box ("Add python.exe to PATH" on recent
installers).

**`Could not find an Oodle library`**
Search your game folders for `oo2core_*_win64.dll` — or, inside an Unreal Engine
install, the unversioned `oo2core.dll` (take the one under a `win-x64` folder,
never `win-x86`) — and copy it next to `patch.py`. See the Setup section for
games known to ship one.

**`No character meshes -- unaffected by V1.005`**
That mod has no character model, so V1.005 didn't break it. Nothing to do.

**A mod reports multiple levels of detail and refuses**
Multi-level models are only refused when they actually need fixing (ones
already in the right format are left alone). Every costume mod tested so far
is single-level, so this should be rare — please report the mod name.

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

1. Converts the per-vertex tangent frame to the new 4-byte encoding (from
   either the 8-byte standard or the 16-byte high-precision form). **This is
   the one that crashes**: the old sizes desync the loader partway through
   the mesh.
2. Converts full-precision texture coordinates to half floats, when a mod uses
   them, because the current shaders read them as half.
3. Removes `FDuplicatedVerticesBuffer` from every render section, which the
   game's own V1.005 meshes no longer carry. On its own this is **not** a
   defect — the engine reads a per-section flag and handles both forms — so
   a mod that only differs here is left alone rather than rewritten.

Converted models stay within **a quarter of a degree** of the artist's original
data — typically 0.06° for normals and 0.10° for tangents, never past 0.25°.

`unpatch.py` reverses all three: the duplicated-vertex arrays return (in the
empty form real 1.004 mods carry), tangents expand back to the 8-byte
standard, and half-float UVs stay — they were always legal.

---

## Licence

MIT — see [LICENSE](LICENSE). Use it, change it, ship it, fold it into your own
tool. Attribution is appreciated but not required.

The licence covers this tool only. It does not cover any mod you use it on, and
no mod content is included or redistributed here.
