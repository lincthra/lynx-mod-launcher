"""Find Steam and Valheim: parses libraryfolders.vdf / appmanifest / localconfig.vdf."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterator

from . import VALHEIM_APPID

log = logging.getLogger(__name__)


class VdfError(ValueError):
    pass


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c, c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            if i >= n:
                raise VdfError("unterminated string")
            i += 1
            yield "str", "".join(buf)
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            word = text[i:j]
            i = j
            if not (word.startswith("[") and word.endswith("]")):  # skip [$WIN32] conditionals
                yield "str", word


def parse_vdf(text: str) -> dict[str, Any]:
    it = _tokens(text)

    def obj(top: bool) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for kind, val in it:
            if kind == "}":
                if top:
                    raise VdfError("unexpected }")
                return d
            if kind != "str":
                raise VdfError("expected key")
            try:
                kind2, val2 = next(it)
            except StopIteration:
                raise VdfError(f"no value for {val!r}") from None
            if kind2 == "{":
                d[val] = obj(False)
            elif kind2 == "str":
                d[val] = val2
            else:
                raise VdfError(f"unexpected {val2!r}")
        if not top:
            raise VdfError("unexpected end of file")
        return d

    return obj(True)


def ci_get(d: Any, *keys: str) -> Any:
    """Case-insensitive nested lookup (Steam isn't consistent about 'Steam' vs 'steam')."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        hit = next((v for kk, v in cur.items() if kk.lower() == k.lower()), None)
        if hit is None:
            return None
        cur = hit
    return cur


def _read_vdf(path: Path) -> dict[str, Any] | None:
    try:
        return parse_vdf(path.read_text("utf-8", errors="replace"))
    except (OSError, VdfError) as e:
        log.debug("couldn't read %s: %s", path, e)
        return None


def _windows_registry_steam() -> list[Path]:
    out: list[Path] = []
    try:
        import winreg
    except ImportError:
        return out
    for hive, sub, name in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as key:
                val, _ = winreg.QueryValueEx(key, name)
                out.append(Path(val))
        except OSError:
            continue
    return out


def steam_root_candidates(platform: str | None = None, home: Path | None = None) -> list[Path]:
    platform = platform or sys.platform
    home = home or Path.home()
    if platform.startswith("win"):
        cands = _windows_registry_steam()
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                cands.append(Path(os.environ[env]) / "Steam")
        cands.append(Path(r"C:\Program Files (x86)\Steam"))
    else:
        cands = [
            home / ".steam" / "steam",
            home / ".steam" / "root",
            home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
            home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
        ]
    return cands


def find_steam_roots(platform: str | None = None, home: Path | None = None) -> list[Path]:
    seen: set[str] = set()
    roots: list[Path] = []
    for c in steam_root_candidates(platform, home):
        try:
            if not (c / "steamapps").is_dir():
                continue
            real = str(c.resolve()).casefold()
        except OSError:
            continue
        if real not in seen:
            seen.add(real)
            roots.append(c)
    return roots


def library_folders(steam_root: Path) -> list[tuple[Path, bool]]:
    """(library path, whether its apps list mentions Valheim). Includes the root itself."""
    libs: list[tuple[Path, bool]] = []
    data = None
    for p in (steam_root / "steamapps" / "libraryfolders.vdf", steam_root / "config" / "libraryfolders.vdf"):
        data = _read_vdf(p)
        if data:
            break
    folders = ci_get(data, "libraryfolders") if data else None
    if isinstance(folders, dict):
        for key, val in folders.items():
            if isinstance(val, dict):  # current format
                path = val.get("path")
                apps = val.get("apps") if isinstance(val.get("apps"), dict) else {}
                if path:
                    libs.append((Path(path), VALHEIM_APPID in apps))
            elif isinstance(val, str) and key.isdigit():  # pre-2021 format
                libs.append((Path(val), False))
    if not any(_same(p, steam_root) for p, _ in libs):
        libs.append((steam_root, False))
    libs.sort(key=lambda t: not t[1])  # the library that claims Valheim first
    return libs


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def is_valheim_dir(path: Path) -> bool:
    return (path / "valheim.exe").is_file() or (path / "valheim.x86_64").is_file()


def find_valheim(roots: list[Path] | None = None) -> Path | None:
    roots = find_steam_roots() if roots is None else roots
    for root in roots:
        for lib, _ in library_folders(root):
            steamapps = lib / "steamapps"
            installdir = "Valheim"
            acf = _read_vdf(steamapps / f"appmanifest_{VALHEIM_APPID}.acf")
            if acf:
                installdir = ci_get(acf, "AppState", "installdir") or installdir
            cand = steamapps / "common" / installdir
            if is_valheim_dir(cand):
                log.info("found Valheim at %s", cand)
                return cand
    return None


def launch_options(steam_root: Path, appid: str = VALHEIM_APPID) -> list[str]:
    """Launch options the user set for the game in Steam (one per Steam account on this PC)."""
    out: list[str] = []
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        return out
    for cfg in userdata.glob("*/config/localconfig.vdf"):
        data = _read_vdf(cfg)
        opts = ci_get(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", appid, "LaunchOptions")
        if isinstance(opts, str):
            out.append(opts)
    return out


def steam_root_for_game(game_dir: Path, roots: list[Path] | None = None) -> Path | None:
    roots = find_steam_roots() if roots is None else roots
    for root in roots:
        for lib, _ in library_folders(root):
            if lib in game_dir.parents or _same(lib, game_dir.parent.parent.parent):
                return root
    return roots[0] if roots else None


def steam_command(steam_root: Path | None, platform: str | None = None) -> list[str] | None:
    """Command prefix that runs the Steam client (used with -applaunch)."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        if steam_root and (steam_root / "steam.exe").is_file():
            return [str(steam_root / "steam.exe")]
        return None
    if steam_root and "com.valvesoftware.Steam" in str(steam_root) and shutil.which("flatpak"):
        return ["flatpak", "run", "com.valvesoftware.Steam"]
    exe = shutil.which("steam")
    if exe:
        return [exe]
    if shutil.which("flatpak"):
        return ["flatpak", "run", "com.valvesoftware.Steam"]
    return None
