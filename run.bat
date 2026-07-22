@echo off
rem FFVII Rebirth Mesh Patcher -- launcher.
rem Opens a command window already inside this folder, so "python patch.py ..."
rem works no matter where you started. That is all it does; every line is here
rem in plain text for you to read.
cd /d "%~dp0"
echo FFVII Rebirth Mesh Patcher   (folder: %cd%)
echo.
echo Commands:
echo     python patch.py --list               list mods and what needs fixing
echo     python patch.py --list --debug       ...with per-mesh detail
echo     python patch.py --all                patch everything that needs it
echo     python patch.py ModName              patch one mod (folder or .utoc name)
echo     python patch.py --restore --all      undo everything, back to unpatched
echo     python patch.py --restore ModName    undo one mod
echo.
cmd /k
