#!/usr/bin/env bash
# Install three desktop/menu shortcuts for the publisher (Linux: KDE, GNOME, etc.):
#   "Publish Modpack"       -> lynx-publish --no-server --confirm   (friends' download only)
#   "Update Valheim Server" -> lynx-publish --server-only --confirm (Pelican server only)
#   "Restart Server When Empty" -> lynx-publish --restart-when-empty (needs [server.restart])
# Each opens a terminal, shows a dry run, asks "Go ahead?", runs, and waits for Enter.
# Re-running this script is safe; it only (re)writes its own files.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
bin_dir="$HOME/.local/bin"
apps_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desk_dir="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
cfg_dir="${XDG_CONFIG_HOME:-$HOME/.config}/lynx-mod-launcher"

publish_bin="$(command -v lynx-publish || true)"
if [[ -z "$publish_bin" ]]; then
  publish_bin="$bin_dir/lynx-publish"
  [[ -x "$publish_bin" ]] || { echo "lynx-publish not found. Run 'pipx install .' first."; exit 1; }
fi

# Shortcuts start in $HOME, so the settings must be in ~/.config. Link the project's copy
# there (a symlink, so editing either one edits the same file).
mkdir -p "$cfg_dir"
if [[ ! -e "$cfg_dir/publish.toml" ]]; then
  if [[ -f "$here/publish.toml" ]]; then
    ln -s "$here/publish.toml" "$cfg_dir/publish.toml"
    echo "Linked $cfg_dir/publish.toml -> $here/publish.toml"
  else
    echo "Note: no publish.toml found. Create $cfg_dir/publish.toml before using the shortcuts."
  fi
fi

mkdir -p "$bin_dir" "$apps_dir"
wrapper="$bin_dir/lynx-shortcut"
cat > "$wrapper" <<EOF
#!/usr/bin/env bash
# Runs lynx-publish for a desktop shortcut and keeps the terminal open afterwards.
cd "\$HOME"
"$publish_bin" "\$@"
status=\$?
echo
if [[ \$status -eq 0 ]]; then echo "Done."; else echo "Finished with errors (exit \$status). See the log above."; fi
read -rp "Press Enter to close..." _
EOF
chmod +x "$wrapper"

make_shortcut() {  # id, title, comment, icon, args
  local file="$apps_dir/$1.desktop"
  cat > "$file" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Exec=$wrapper $5
Icon=$4
Terminal=true
Categories=Game;Utility;
EOF
  chmod +x "$file"
  echo "Installed menu entry: $2"
  if [[ -d "$desk_dir" ]]; then
    cp "$file" "$desk_dir/"
    chmod +x "$desk_dir/$1.desktop"
    gio set "$desk_dir/$1.desktop" metadata::trusted true 2>/dev/null || true
    echo "  and desktop icon: $desk_dir/$1.desktop"
  fi
}

make_shortcut lynx-publish-modpack "Publish Modpack" \
  "Publish the Gale profile so friends' launchers update" "document-send" "--no-server --confirm"
make_shortcut lynx-update-server "Update Valheim Server" \
  "Copy updated server plugins to the Pelican server" "network-server" "--server-only --confirm"

make_shortcut lynx-restart-when-empty "Restart Server When Empty" \
  "Restart the Valheim server once nobody is on it" "view-refresh" "--restart-when-empty"

command -v update-desktop-database >/dev/null && update-desktop-database "$apps_dir" 2>/dev/null || true
echo "Done. (KDE may ask you to trust the desktop icons the first time you click them.)"
