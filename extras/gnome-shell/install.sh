#!/usr/bin/env bash
# Install the taq GNOME Shell extension.
#
# The shell only scans its extension directories at startup, and on Wayland it
# cannot be restarted without logging out — so this copies the files, registers
# the uuid, and tells you the one thing left to do.
set -euo pipefail

UUID="taq-clock@amirsalmani.com"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/$UUID" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"

# A symlink here does not work: the shell's scanner does not follow them.
rm -rf "$DEST"
mkdir -p "$DEST"
cp "$SRC"/extension.js "$SRC"/metadata.json "$SRC"/stylesheet.css "$DEST"/
echo "  installed to $DEST"

python3 - "$UUID" <<'PY'
import ast, subprocess, sys
uuid = sys.argv[1]
cur = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
                     capture_output=True, text=True).stdout.strip()
try:
    lst = ast.literal_eval(cur) if cur.startswith("[") else []
except Exception:
    lst = []
if uuid in lst:
    print("  already registered")
else:
    lst.append(uuid)
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions",
                    "[" + ", ".join(f"'{x}'" for x in lst) + "]"], check=True)
    print("  registered in org.gnome.shell enabled-extensions")
PY

echo
echo "  Log out and back in. The shell scans for new extensions only at startup,"
echo "  and Wayland has no way to restart it in place."
echo
echo "  Zone defaults to Europe/Helsinki. To change it:"
echo "      mkdir -p ~/.config/taq && echo 'Europe/Berlin' > ~/.config/taq/second-zone"
echo "  Then: gnome-extensions disable $UUID && gnome-extensions enable $UUID"
