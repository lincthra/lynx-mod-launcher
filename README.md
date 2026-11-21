# Lynx Mod Launcher

Keeps friends' Valheim mods identical to a Gale profile on your PC. Friends get one file:
open it, wait for "Updated N files" / "Up to date", click **Launch**.

```
 Gale profile (any name)  ──lynx-publish──►  GitHub Releases (public repo)   ──►  LynxModLauncher (friends)
  real DLLs + configs                         manifest  +  every file by hash        own mod folder → doorstop → Valheim
                          └─(optional) --server──►  Pelican Valheim server  BepInEx/plugins
```

The publisher uploads the actual files from your Gale profile folder: every DLL, asset
bundle and config. `manifest.json` is only the index (path, size, SHA-256) that tells the
launcher which of them it's missing. Nothing is fetched from Thunderstore, so the
published copy is the source of truth.

## Storage: GitHub Releases (recommended) or your own web server

`dest = "github:OWNER/REPO"` stores everything as release assets on a **public** repo:

- Each file is an asset named by its SHA-256, spread over 16 releases (`objects-0` … `objects-f`).
  That stays well under GitHub's 1000-assets-per-release limit.
- `manifest.json` lives in the `manifest` release. It's replaced last, after all files are up.
- Republishing uploads only files whose content changed. `--prune` deletes assets that are
  no longer used.
- The launcher's manifest URL is `https://github.com/OWNER/REPO/releases/download/manifest/manifest.json`.
  The publisher prints it after each run.

Notes:
- The repo needs one commit (e.g. a README) before releases can be created.
- Release downloads have no API rate limit, so friends don't need GitHub accounts.
- Private repos aren't supported, because the launcher would need a token. Public means
  anyone with the repo name can download the mod DLLs.
- Use `gh` on CachyOS: `sudo pacman -S github-cli && gh auth login`.

You can still use a folder served by your own web server (`dest = "/srv/www/modpack"`), or
one reached over rsync/SSH (`dest = "user@host:/srv/www/modpack"`).

## What the launcher changes on a friend's PC

- **Mod folder:** the launcher's own, not Valheim's.
  - Windows: `%LOCALAPPDATA%\LynxModLauncher\profile`
  - Linux: `~/.local/share/lynx-mod-launcher/profile`
- **Update:**
  1. Compare files against the manifest. It checks sizes first and keeps a size+mtime hash
     cache, so normal launches are instant.
  2. Download what changed into a staging folder and check each file's SHA-256.
  3. Apply. Nothing in the mod folder changes unless **every** download checks out.
- **Deleting:**
  - `BepInEx/plugins`, `patchers` and `core` are mirrored exactly. Extra files there are removed.
  - Everywhere else, including `BepInEx/config`, the launcher only removes files it installed
    and you've since dropped from the pack. Files that mods create on the friend's PC are never
    touched (SeneaL-UI stats, QuickStackStore data, server passwords).
  - Files matching **keep** patterns are installed once, then left alone.
- **Can't reach the server, or the update fails:** Launch still works with what's there.
- **Valheim folder:** it only gets `winhttp.dll` plus a `doorstop_config.ini` with doorstop
  **disabled**. Each launch enables doorstop and points it at the mod folder, so starting
  Valheim from Steam stays vanilla. Existing files there are backed up once as `*.lynx-bak`.
- **Log:** `…/logs/launcher.log`. The **Open logs** button opens the folder.

| Platform | How it launches |
|---|---|
| Windows | `steam.exe -applaunch 892970 --doorstop-enabled true --doorstop-target-assembly …\BepInEx\core\BepInEx.Preloader.dll` (`"launch_mode": "direct"` in `settings.json` runs `valheim.exe` directly) |
| Linux native | `valheim.x86_64` with `LD_PRELOAD=<mod folder>/libdoorstop.so` and doorstop env vars |
| Linux Proton | `steam -applaunch 892970` with `Z:\…` paths. Steam's launch options need `WINEDLLOVERRIDES="winhttp=n,b" %command%`, which the launcher checks for and offers to copy. |

Steam is found via the Windows registry, `~/.steam`, `~/.local/share/Steam`, Flatpak or
Snap, then `libraryfolders.vdf`. If that fails, the launcher asks for the folder once.

### After clicking Launch

The launcher stays open as a splash screen. It shows the logo and follows BepInEx's
`LogOutput.log`, so you see "Waiting for Steam…", then "Loading mods… 23 of 60" with the
current mod's name. It closes once BepInEx reports "Chainloader startup complete".
- **Progress bar:** the mod count comes from the previous launch.
- **Nothing happening:** after 90 seconds without any sign of Valheim it says so, and keeps watching.
- **Buttons:** "Open game log" opens BepInEx's log folder, and "Close" dismisses the splash
  at any time. It never touches the game.

### Self-update

Every tagged release also publishes `latest.json` (version and SHA-256 for each exe). On start,
the launcher checks it. If there's a newer version, it downloads it, verifies the hash, swaps
the exe and restarts. Friends never re-download by hand. A failed or offline check is logged
and ignored.
- **Windows:** the old exe is renamed to `LynxModLauncher.exe.old` and deleted on the next start.
- **Opting out:** `"auto_update": "no"` in `settings.json`, or `--no-self-update`.
- **Running from source:** self-update is skipped.

Two launcher windows never update at once: the second waits for the first (profile lock).

### Command line (testing, Wine)

```
LynxModLauncher --sync-only                 # update, no window
LynxModLauncher --headless --print-launch   # update and show the launch command without starting
  --manifest-url URL   --game-dir DIR   --data-dir DIR   --profile-dir DIR   -v
```
The env vars `LYNX_MANIFEST_URL` and `LYNX_HOME` work too.

## Publishing

```
pipx install .   # on CachyOS (pip refuses system-wide installs); or: python -m lynxlauncher.publisher
mkdir -p ~/.config/lynx-mod-launcher && cp publish.example.toml ~/.config/lynx-mod-launcher/publish.toml

lynx-publish --dry-run --list          # what would ship
lynx-publish                           # publish the profile named in the settings
lynx-publish --profile NextPlaythrough # publish a different Gale profile
lynx-publish --server                  # also mirror BepInEx/plugins to the Pelican server
lynx-publish --prune                   # also delete files no longer used
lynx-publish --server-only             # only update the Pelican server
lynx-publish --confirm                 # dry run first, then ask "Go ahead?"
```

A dry run lists exactly which files would be uploaded, or says everything is already published.

### Desktop shortcuts (Linux)

`scripts/install-shortcuts.sh` adds **Publish Modpack** (`--no-server --confirm`) and
**Update Valheim Server** (`--server-only --confirm`) to the app menu and the desktop.
Each opens a terminal, shows the dry run, asks before doing anything, and waits for Enter at
the end. Shortcuts start in your home folder, so the script links the project's `publish.toml` into
`~/.config/lynx-mod-launcher/`.

- The settings take a Gale **profile name**, looked up in
  `~/.local/share/com.kesomannen.gale/valheim/profiles/`. You can use `source = "/any/folder"` instead.
- Logs, `*.old`, `*.bak`, `BepInEx/cache` and Gale's metadata are never published.
- The example settings also exclude the per-character and per-world data and the server
  password found in the PridelandsPoV profile.
- **Secrets guard:** a file whose name looks like a password, token or key stops the publish
  until you exclude it or list it in `allow_secrets`.
- Every run writes a full log to `~/.local/state/lynx-mod-launcher/publish-logs/` (newest 30 kept): the complete file list and every rsync line. The terminal shows only a summary and real changes, and ends with the log's path.
- Close Gale and the game while publishing. If a file changes mid-publish, it stops.
- The server push leaves extra server files alone unless `delete = true`. Restart the server
  afterwards.

## Building

Push to GitHub (this code can live in its own repo, separate from the modpack storage repo).
The workflow tests on Windows and Linux and builds one-file binaries with PyInstaller.

- Set the repository variable **`MANIFEST_URL`** (Actions → Variables) to bake in the real URL.
- Tag `v0.1.0` to create a release with `LynxModLauncher.exe` and `LynxModLauncher-linux`
  attached. The Linux build runs fine on CachyOS.

Running the launcher from source on CachyOS needs Tk: `sudo pacman -S tk`.

Tests run without Valheim (local HTTP server and a fake `gh`): `pip install pytest && pytest`
