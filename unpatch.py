"""
unpatch.py -- FFVII Rebirth mesh patcher, in reverse.

Converts mods BACK to the pre-V1.005 mesh format, for players who stayed on
(or rolled back to) game version 1.004. The mirror image of patch.py, and used
exactly the same way -- drop a mod folder or archive onto it, or:

    python unpatch.py --list             show every mod and whether it needs it
    python unpatch.py --all              unpatch everything that needs it
    python unpatch.py ModName            unpatch specific mods by name
    python unpatch.py --restore --all    undo everything, from the backups

    python unpatch.py --path "D:\\mods"           work on a folder instead
    python unpatch.py --path "D:\\mods" --out "D:\\send"   copies, originals kept

WHAT IT DOES
------------
The reverse of patch.py's three fixes, measured from real pre-1.005 mods:

  1. Each render section gets its FDuplicatedVerticesBuffer back, in the empty
     form real 1.004 mods already carry ("no vertex has duplicates") -- the
     original arrays were stale donor data with no recoverable meaning.
  2. The 4-byte tangent encoding expands back to the standard 8-byte
     FPackedNormal pair the 1.004 game reads.
  3. Texture coordinates stay as they are: half floats were always legal.

In-place runs back up originals to ./unpatch_backups/<ModName>/ (folder drops
back up inside the folder, to _unpatch_backups); --restore undoes from there.
Everything else -- where mods are found, drops, archives, --path/--out -- is
patch.py's machinery, unchanged.
"""

import os
import sys

import patch

patch.MODE = patch.BACKWARD
patch.BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "unpatch_backups")

if __name__ == "__main__":
    sys.exit(patch.run(sys.argv[1:]))
