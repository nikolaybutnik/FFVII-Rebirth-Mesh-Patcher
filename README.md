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

1. **Install Python 3.9 or newer** from <https://www.python.org/downloads/>.
   If the installer shows a PATH tickbox — **"Add python.exe to PATH"** on
   recent versions, "Add Python 3.x to PATH" on older ones — tick it; missing
   it is the usual cause of `'python' is not recognized`. Newer Windows
   installs use the **Python install manager**, which doesn't do that by
   default: there, type **`py`** wherever this README says `python`.
2. **Extract this tool**, then open a terminal in its folder — type `cmd` in
   File Explorer's address bar while in the folder, or right-click the folder
   and choose "Open in Terminal". Run `pip install numpy`. Once, ever. (If that
   says `'pip' is not recognized`, use `python -m pip install numpy` — or
   `py -m pip install numpy` with the install manager.)
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
executable, so there is no copy in the game folder to borrow, and it is
proprietary so it cannot be bundled here — but it ships as a loose
`oo2core_*_win64.dll` with a number of games. You need **oo2core_6 or newer**.
The tool looks beside itself, then through your Steam, Epic and GOG games and
any Unreal Engine install.

Only a minority of games include one, but they tend to be big titles, so
there's a fair chance you already have it. Games reported to ship a working
copy — search the game's folder for `oo2core`, as the file name and location
vary:

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

If you have none of those, **Unreal Engine ships one** and is free from the Epic
Games Launcher — a large download for one file, but it always works. The tool
usually finds it; if not, search the engine folder for `oo2core.dll` (recent) or
`oo2core_*_win64.dll` (older) and take the copy under a **`win-x64`** folder,
never `win-x86`.

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
terminal, no flags. It lists what it found, then offers to patch everything
into a `Patched Mods` folder beside the original; your originals are never
touched. Drop as many as you like at once, and each may hold several mods, or
archives inside archives — all of it is unpacked. `.7z` and `.rar` need nothing
installed. An archive is looked inside before anything is offered, so one whose
mods are already patched says so instead.

Two naming rules are handled for you. The `.utoc`/`.ucas`/`.pak` files keep
their exact names, because the loader would lose track of a renamed mod. And a
Dresscode mod's folder is renamed to match the `.uplugin` inside it — Dresscode
looks a mod up by folder name and silently ignores one that doesn't match,
which is how some downloads arrive. Correctly packaged mods are left alone.

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

Only mod files are ever modified, never the game's own files. Pak mods
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

**Every `patch.py` command works on `unpatch.py` unchanged** — mod names,
`--restore`, `--path`/`--out`, `--debug`, drag-and-drop of folders and
archives, all of it. The only difference is where backups go: in-place runs
back up to `unpatch_backups\` (folder drops to `_unpatch_backups\` inside
the folder), and each tool's `--restore` undoes its own work from there.
Patching and unpatching are mutual inverses: a mod taken down to 1.004 and
back comes out identical, apart from tangent rounding far below anything
visible.

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
`Optional` paks, and a `dresscode.json` is written beside them. Weapons-menu
rows convert too, one pak each. That file remembers the original, so
converting the folder back later rebuilds it exactly. **Leave
`dresscode.json` where it is** if you ever want the round trip.

### Paks → Dresscode: how to organize the folder

Put ONE mod in one folder and drop that folder. First drop writes
`dresscode.json` (open it to set names, or don't); second drop builds.

**Set it up like this.** The converter accepts most shapes, but this is the
layout I'd recommend — it keeps things clear, and makes any problem easier
to sort out.

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

That gives you a single Dresscode entry with a tile per costume. Use it
even when the mod has only one costume — `Variants\Standard\` on its own
is fine, and keeps every mod you convert looking the same.

Habits that save trouble — none of them required:

- **Name the folders the way you want the tiles named** — those names go
  straight into the menu. Keep them short; very long ones get shortened.
- **Unpack down to the three files** (`.utoc`, `.ucas`, `.pak`). A download
  often arrives with `~mods\` or `Content\Paks\WindowsNoEditor\` inside; lift
  the files out. Both work, but the flat one is simplest.
- **Nothing loose at the top** but `icon.png` and `dresscode.json`.
- **One copy of each add-on** in `Optional\`, never a copy inside each variant.
- **Tidying is for your sake, not the tool's.** Paks are found however many
  folders deep they sit, and `~mods\`/`Content\Paks\` wrappers are ignored when
  naming tiles, so an untouched download converts fine.

**Why things go where — is this pak a whole costume, or a change to one?**

A **whole costume** replaces the model — a different body, a hairstyle, a
part modelled away. Those go under `Variants\`. A **change** only repaints
or hides what the costume already has (a recolour, a hidden buckle).
Those go under `Optional\`. Get it wrong and the converter says so: a
costume filed under `Optional\` is reported as "replaces the model, not
just materials".

| What you have | Where it goes | What you get |
| --- | --- | --- |
| every whole costume | `Variants\<name>\` | one tile each, all in one Dresscode mod |
| changes to a costume | `Optional\<name>\` | listed for you to combine — see below |
| a pak the costume NEEDS (its textures or materials, shipped separately) | beside that costume | merged in automatically |
| whole costumes you want as **separate mods** instead | any other subfolder | a Dresscode mod per costume |
| a weapon mod, with no costume in it | the weapon paks, loose in the folder | one WEAPONS-menu tile each |

Pre-V1.005 mods are caught for you: `convert.py` says so before doing anything
and offers to patch them right there, backups kept.

### Making your own tiles

A fresh conversion lists every option pak under
`"parts_you_can_combine"` and leaves `"variants"` for you to fill in —
option paks do NOT become tiles on their own. That's deliberate: **tiles
don't stack in game**. Pick "Red" and then "No Hat" and you'd get a
hatless outfit in the normal colour, because the second tile replaces the
first — a tile per option would just be clutter that works wrong.
(Weapon paks are the exception: their tile stands alone in the WEAPONS
menu, so their entries are written for you.)

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
- An entry goes on every outfit. Add `"outfit": "Standard"` (or a list of
  names) to put one on only some of them. That is for costume add-ons
  only — a weapon entry is not worn with a costume, so `"outfit"` does
  nothing on one.
- The menu shows exactly this list: rename tiles, delete the ones you
  never use, or replace the whole list with a few favourite combos.
- Made a mess of the file? Delete `dresscode.json` and drop the folder
  again for a fresh one. (Only do that if the file has no `"restore"`
  section — that section is the mod's way back to its original form.)

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
  they are wearing. A weapon mod therefore needs no costume — drop a folder of
  weapon paks on their own and each weapon becomes a tile, including when one
  pak covers a character's whole set; `"outfits"` stays empty and the menu is
  written for you. A weapon that ships its own model converts with nothing
  installed; one that only recolours the stock model borrows it from the game,
  so that pak needs the game and is otherwise skipped with a note and keeps
  working from `~mods`. Coming back, a tile this tool made returns to an
  override pak by itself, while a weapon mod someone else wrote for Dresscode
  needs the game (or Dresscode) installed to look up which weapon it stands in
  for — one whose files carry no id is skipped with a note rather than guessed
  at.
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
  there is weapon paks.

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
- **Drop the whole mod folder**, not one pak at a time. When a mod comes as
  several paks — a main one plus optional extras — the extras override files
  that sit inside the costume's folder. Move the main pak and those files move
  with it, so an extra left on the old costume is pointing at files nothing
  uses any more, and quietly does nothing. Dropping the folder asks you once
  and moves all of them together.
- **It tells you what won't carry over.** Some mods replace one of the game's
  own files — a skin texture, say — that only exists on the costume they were
  built for. Move the mod and the game has no such file on the new costume, so
  that one replacement stops doing anything. The outfit still works; the tool
  just lists what dropped out, so a small unexplained difference isn't a
  mystery. This is the one part that needs the game installed.
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
