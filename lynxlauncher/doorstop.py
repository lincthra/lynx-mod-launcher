"""BepInEx doorstop setup and the launch command for each platform.

The mods live in the launcher's own profile folder, not in the Valheim folder. Doorstop is
pointed at that folder per launch (command-line args on Windows/Proton, environment
variables on Linux native). The doorstop_config.ini written into the Valheim folder has
doorstop *disabled*, so starting Valheim normally from Steam stays vanilla.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import VALHEIM_APPID

log = logging.getLogger(__name__)

PRELOADER_REL = ("BepInEx", "core", "BepInEx.Preloader.dll")
BACKUP_SUFFIX = ".lynx-bak"
PROTON_LAUNCH_OPTIONS = 'WINEDLLOVERRIDES="winhttp=n,b" %command%'

GAME_DIR_INI = """\
# Written by Lynx Mod Launcher.
# Doorstop is disabled here on purpose: starting Valheim from Steam stays vanilla.
# The launcher enables it per launch and points it at its own mod folder.
[General]
enabled = false
target_assembly = BepInEx\\core\\BepInEx.Preloader.dll

[UnityDoorstop]
enabled=false
targetAssembly=BepInEx\\core\\BepInEx.Preloader.dll
"""


class DoorstopError(RuntimeError):
    pass


class GameKind(enum.Enum):
    WINDOWS = "windows"
    LINUX_NATIVE = "linux-native"
    LINUX_PROTON = "linux-proton"


@dataclass
class DoorstopInfo:
    version: int  # 3 or 4
    target: Path
    corlib: Path | None
    winhttp: Path | None
    linux_lib: Path | None


@dataclass
class LaunchSpec:
    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)  # full environment for the child
    kind: GameKind = GameKind.WINDOWS
    warnings: list[str] = field(default_factory=list)


def _ini_value(text: str, *keys: str) -> str | None:
    wanted = {k.lower() for k in keys}
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;[" or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip().lower() in wanted:
            return v.strip().strip('"')
    return None


def _find_linux_lib(profile: Path) -> Path | None:
    for rel in ("doorstop_libs/libdoorstop_x64.so", "doorstop_libs/libdoorstop.so", "libdoorstop.so", "libdoorstop_x64.so"):
        p = profile / rel
        if p.is_file():
            return p
    found = sorted(profile.rglob("libdoorstop*.so"), key=lambda p: ("x86" in p.name and "x64" not in p.name, str(p)))
    return found[0] if found else None


def detect_doorstop(profile: Path) -> DoorstopInfo:
    profile = Path(profile).absolute()
    target = profile.joinpath(*PRELOADER_REL)
    if not target.is_file():
        raise DoorstopError(
            "The mods haven't been downloaded yet (BepInEx is missing). "
            "Check your internet connection and press Check again."
        )
    ini = profile / "doorstop_config.ini"
    text = ini.read_text("utf-8", errors="replace") if ini.is_file() else ""
    version = None
    vfile = profile / ".doorstop_version"
    if vfile.is_file():
        m = re.match(r"\s*(\d+)", vfile.read_text("utf-8", errors="replace"))
        if m:
            version = int(m.group(1))
    if version is None:
        version = 3 if re.search(r"^\s*\[UnityDoorstop\]", text, re.M | re.I) else 4
    corlib = None
    raw = _ini_value(text, "dll_search_path_override", "dllSearchPathOverride")
    if raw:
        cand = profile.joinpath(*[p for p in re.split(r"[\\/]", raw) if p])
        if cand.is_dir():
            corlib = cand
    if corlib is None and (profile / "unstripped_corlib").is_dir():
        corlib = profile / "unstripped_corlib"
    winhttp = profile / "winhttp.dll"
    return DoorstopInfo(version, target, corlib, winhttp if winhttp.is_file() else None, _find_linux_lib(profile))


def detect_game_kind(game_dir: Path, platform: str | None = None) -> GameKind:
    platform = platform or sys.platform
    game_dir = Path(game_dir)
    if platform.startswith("win"):
        if (game_dir / "valheim.exe").is_file():
            return GameKind.WINDOWS
    elif (game_dir / "valheim.x86_64").is_file():
        return GameKind.LINUX_NATIVE
    elif (game_dir / "valheim.exe").is_file():
        return GameKind.LINUX_PROTON
    raise DoorstopError(f"Valheim isn't installed in {game_dir}")


def to_wine_path(path: Path | str) -> str:
    p = PurePosixPath(str(path).replace("\\", "/"))
    if not p.is_absolute():
        raise ValueError(f"need an absolute path: {path}")
    return "Z:" + str(p).replace("/", "\\")


def doorstop_args(info: DoorstopInfo, path_fn=str) -> list[str]:
    target = path_fn(info.target)
    corlib = path_fn(info.corlib) if info.corlib else None
    if info.version == 3:
        args = ["--doorstop-enable", "true", "--doorstop-target", target]
        if corlib:
            args += ["--mono-dll-search-path-override", corlib]
    else:
        args = ["--doorstop-enabled", "true", "--doorstop-target-assembly", target]
        if corlib:
            args += ["--doorstop-mono-dll-search-path-override", corlib]
    return args


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _backup_once(path: Path) -> None:
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)
        log.info("backed up %s", path)


def prepare_game_dir(game_dir: Path, info: DoorstopInfo, kind: GameKind) -> None:
    """Install/refresh winhttp.dll + a disabled doorstop_config.ini in the Valheim folder."""
    if kind is GameKind.LINUX_NATIVE:
        return  # LD_PRELOAD from the profile, nothing to put in the game folder
    if info.winhttp is None:
        raise DoorstopError("winhttp.dll is missing from the downloaded mods.")
    game_dir = Path(game_dir)
    try:
        dst = game_dir / "winhttp.dll"
        if not dst.is_file() or _sha(dst) != _sha(info.winhttp):
            _backup_once(dst)
            tmp = dst.with_name(dst.name + ".lynx-tmp")
            shutil.copyfile(info.winhttp, tmp)
            os.replace(tmp, dst)
            log.info("installed doorstop winhttp.dll into %s", game_dir)
        ini = game_dir / "doorstop_config.ini"
        current = ini.read_text("utf-8", errors="replace") if ini.is_file() else None
        if current != GAME_DIR_INI:
            _backup_once(ini)
            ini.write_text(GAME_DIR_INI, "utf-8")
    except PermissionError as e:
        raise DoorstopError(f"Couldn't update files in {game_dir}. Is Valheim already running?") from e


def child_env(base: dict[str, str] | None = None, frozen: bool | None = None) -> dict[str, str]:
    """Environment for the game/Steam without PyInstaller's bundle overrides.

    A PyInstaller one-file build points LD_LIBRARY_PATH at its temp folder (saving the
    user's value as LD_LIBRARY_PATH_ORIG). Steam or Valheim inheriting that would load
    the launcher's bundled libraries, so put the original back.
    """
    env = dict(os.environ if base is None else base)
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        env["LD_LIBRARY_PATH"] = orig
    elif frozen:
        env.pop("LD_LIBRARY_PATH", None)
    if frozen:
        for var in ("TCL_LIBRARY", "TK_LIBRARY"):
            env.pop(var, None)
    return env


def build_launch(
    kind: GameKind,
    game_dir: Path,
    info: DoorstopInfo,
    steam_cmd: list[str] | None,
    mode: str = "auto",
    base_env: dict[str, str] | None = None,
) -> LaunchSpec:
    game_dir = Path(game_dir)
    env = child_env(base_env, frozen=False if base_env is not None else None)

    if kind is GameKind.LINUX_NATIVE:
        if info.linux_lib is None:
            raise DoorstopError("The Linux doorstop library (libdoorstop*.so) is missing from the mods.")
        target = str(info.target)
        env.update({
            "SteamAppId": VALHEIM_APPID,
            "SteamGameId": VALHEIM_APPID,
            # doorstop 4
            "DOORSTOP_ENABLED": "1",
            "DOORSTOP_TARGET_ASSEMBLY": target,
            # doorstop 3 (what BepInExPack_Valheim's start_game_bepinex.sh uses)
            "DOORSTOP_ENABLE": "TRUE",
            "DOORSTOP_INVOKE_DLL_PATH": target,
        })
        if info.corlib:
            env["DOORSTOP_MONO_DLL_SEARCH_PATH_OVERRIDE"] = str(info.corlib)
            env["DOORSTOP_CORLIB_OVERRIDE_PATH"] = str(info.corlib)
        libpath = [str(info.linux_lib.parent), str(game_dir)]
        if env.get("LD_LIBRARY_PATH"):
            libpath.append(env["LD_LIBRARY_PATH"])
        env["LD_LIBRARY_PATH"] = ":".join(libpath)
        preload = [str(info.linux_lib)] + ([env["LD_PRELOAD"]] if env.get("LD_PRELOAD") else [])
        env["LD_PRELOAD"] = ":".join(preload)
        return LaunchSpec([str(game_dir / "valheim.x86_64")], str(game_dir), env, kind)

    if kind is GameKind.LINUX_PROTON:
        if not steam_cmd:
            raise DoorstopError("Couldn't find the Steam client to start Valheim through Proton.")
        args = doorstop_args(info, to_wine_path)
        return LaunchSpec([*steam_cmd, "-applaunch", VALHEIM_APPID, *args], None, env, kind)

    # Windows
    args = doorstop_args(info)
    if mode == "direct" or not steam_cmd:
        return LaunchSpec([str(game_dir / "valheim.exe"), *args], str(game_dir), env, kind)
    return LaunchSpec([*steam_cmd, "-applaunch", VALHEIM_APPID, *args], None, env, kind)


def start(spec: LaunchSpec) -> subprocess.Popen:
    log.info("launching: %s (cwd=%s)", subprocess.list2cmdline(spec.argv), spec.cwd)
    kwargs: dict = {"cwd": spec.cwd, "env": spec.env, "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(spec.argv, **kwargs)
