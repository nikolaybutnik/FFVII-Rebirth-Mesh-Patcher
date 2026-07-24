@echo off
rem FFVII Rebirth Mesh Patcher -- launcher.
rem Opens a command window already inside this folder, so "python patch.py ..."
rem works no matter where you started. That is all it does; every line is here
rem in plain text for you to read.
cd /d "%~dp0"
echo FFVII Rebirth Mesh Patcher   (folder: %cd%)
echo.
echo Standard commands:
echo     python patch.py --list               list mods and what needs fixing
echo     python patch.py --list --debug       ...with per-mesh detail
echo     python patch.py --all                patch everything that needs it
echo     python patch.py ModName              patch one mod (folder or .utoc name)
echo     python patch.py --restore --all      undo everything, back to unpatched
echo     python patch.py --restore ModName    undo one mod
echo.
echo Advanced commands:
echo     python patch.py --path "D:\my mods"                  list that folder
echo     python patch.py --path "D:\my mods" --all            patch it in place
echo     python patch.py --path "D:\my mods" ModName          patch just one
echo     python patch.py --path "D:\my mods" --out "D:\send"  patched COPIES to
echo                                                           --out, originals kept
echo     python patch.py --restore --all --path "D:\my mods"  undo that folder
echo.
echo     Tip: you can also just drag mod folders -- or .zip/.7z/.rar archives,
echo     even one containing several mods -- onto patch.py.
echo     --path needs no game installed, only the Oodle DLL.
echo.
cmd /k
