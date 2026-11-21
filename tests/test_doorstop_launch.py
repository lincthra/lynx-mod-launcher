import os
import sys
from pathlib import Path

import pytest

from lynxlauncher import doorstop
from lynxlauncher.doorstop import GameKind
from lynxlauncher.launcher_app import LauncherCore

from .conftest import write

posix_only = pytest.mark.skipif(os.name == "nt", reason="Linux launch paths")


def test_detect_v4_with_corlib(gale_profile):
    info = doorstop.detect_doorstop(gale_profile)
    assert info.version == 4
    assert info.corlib == (gale_profile / "unstripped_corlib").absolute()
    assert info.winhttp and info.linux_lib.name == "libdoorstop.so"


def test_detect_v3_from_ini(gale_profile):
    (gale_profile / ".doorstop_version").unlink()
    (gale_profile / "doorstop_config.ini").write_text("[UnityDoorstop]\nenabled=true\ndllSearchPathOverride=unstripped_corlib\n")
    info = doorstop.detect_doorstop(gale_profile)
    assert info.version == 3 and info.corlib is not None
    args = doorstop.doorstop_args(info)
    assert args[:2] == ["--doorstop-enable", "true"] and "--mono-dll-search-path-override" in args


def test_missing_bepinex(tmp_path):
    with pytest.raises(doorstop.DoorstopError, match="haven't been downloaded"):
        doorstop.detect_doorstop(tmp_path)


def test_windows_launch_via_steam(gale_profile, tmp_path):
    game = tmp_path / "Valheim"
    write(game, "valheim.exe", b"")
    info = doorstop.detect_doorstop(gale_profile)
    spec = doorstop.build_launch(GameKind.WINDOWS, game, info, ["C:/Steam/steam.exe"], base_env={})
    assert spec.argv[:3] == ["C:/Steam/steam.exe", "-applaunch", "892970"]
    assert spec.argv[3:7] == ["--doorstop-enabled", "true", "--doorstop-target-assembly", str(info.target)]
    assert spec.argv[-1] == str(info.corlib)
    direct = doorstop.build_launch(GameKind.WINDOWS, game, info, ["steam.exe"], mode="direct", base_env={})
    assert direct.argv[0] == str(game / "valheim.exe") and direct.cwd == str(game)


@posix_only
def test_proton_uses_wine_paths(gale_profile, tmp_path):
    info = doorstop.detect_doorstop(gale_profile)
    spec = doorstop.build_launch(GameKind.LINUX_PROTON, tmp_path, info, ["steam"], base_env={})
    target = spec.argv[spec.argv.index("--doorstop-target-assembly") + 1]
    assert target == "Z:" + str(info.target).replace("/", "\\")
    assert spec.argv[:3] == ["steam", "-applaunch", "892970"]


@posix_only
def test_linux_native_env(gale_profile, tmp_path):
    info = doorstop.detect_doorstop(gale_profile)
    spec = doorstop.build_launch(GameKind.LINUX_NATIVE, tmp_path, info, None,
                                 base_env={"LD_PRELOAD": "libother.so", "HOME": "/home/x"})
    env = spec.env
    assert spec.argv == [str(tmp_path / "valheim.x86_64")]
    assert env["LD_PRELOAD"] == f"{info.linux_lib}:libother.so"
    assert env["LD_LIBRARY_PATH"].split(":")[:2] == [str(info.linux_lib.parent), str(tmp_path)]
    assert env["DOORSTOP_TARGET_ASSEMBLY"] == env["DOORSTOP_INVOKE_DLL_PATH"] == str(info.target)
    assert env["DOORSTOP_CORLIB_OVERRIDE_PATH"] == str(info.corlib)
    assert env["SteamAppId"] == "892970" and env["HOME"] == "/home/x"


def test_child_env_undoes_pyinstaller():
    env = doorstop.child_env({"LD_LIBRARY_PATH": "/tmp/_MEI123", "LD_LIBRARY_PATH_ORIG": "/usr/local/lib"}, frozen=True)
    assert env["LD_LIBRARY_PATH"] == "/usr/local/lib" and "LD_LIBRARY_PATH_ORIG" not in env
    env = doorstop.child_env({"LD_LIBRARY_PATH": "/tmp/_MEI123", "TCL_LIBRARY": "x"}, frozen=True)
    assert "LD_LIBRARY_PATH" not in env and "TCL_LIBRARY" not in env


def test_prepare_game_dir_is_nondestructive(gale_profile, tmp_path):
    game = tmp_path / "Valheim"
    write(game, "valheim.exe", b"")
    write(game, "winhttp.dll", b"someone else's doorstop")
    write(game, "doorstop_config.ini", "[General]\nenabled = true\n")
    info = doorstop.detect_doorstop(gale_profile)
    doorstop.prepare_game_dir(game, info, GameKind.WINDOWS)
    assert (game / "winhttp.dll").read_bytes() == b"winhttp v4"
    assert (game / "winhttp.dll.lynx-bak").read_bytes() == b"someone else's doorstop"
    assert "enabled = false" in (game / "doorstop_config.ini").read_text()
    assert (game / "doorstop_config.ini.lynx-bak").read_text() == "[General]\nenabled = true\n"
    # second run: no new backups, no changes
    doorstop.prepare_game_dir(game, info, GameKind.WINDOWS)
    assert (game / "winhttp.dll.lynx-bak").read_bytes() == b"someone else's doorstop"


def test_game_kind(tmp_path):
    write(tmp_path, "valheim.exe", b"")
    assert doorstop.detect_game_kind(tmp_path, "win32") is GameKind.WINDOWS
    assert doorstop.detect_game_kind(tmp_path, "linux") is GameKind.LINUX_PROTON
    write(tmp_path, "valheim.x86_64", b"")
    assert doorstop.detect_game_kind(tmp_path, "linux") is GameKind.LINUX_NATIVE


def test_core_sync_then_prepare(do_publish, server, tmp_path):
    do_publish()
    game = tmp_path / "Valheim"
    write(game, "valheim.exe" if sys.platform.startswith("win") else "valheim.x86_64", b"")
    core = LauncherCore(server, data_dir=tmp_path / "data", game_dir=game)
    assert not core.can_launch()
    assert core.sync().ok
    assert core.can_launch()
    spec = core.prepare_launch()
    assert spec.argv
