from pathlib import Path

import pytest

from lynxlauncher import steam
from lynxlauncher.paths import PathMatcher

from .conftest import write


@pytest.mark.parametrize("pattern,path,expected", [
    ("*.log", "BepInEx/LogOutput.log", True),
    ("*.log", "a.log.txt", False),
    ("BepInEx/config/*.cfg", "BepInEx/config/x.cfg", True),
    ("BepInEx/config/*.cfg", "BepInEx/config/sub/x.cfg", False),
    ("BepInEx/config/**/*.cfg", "BepInEx/config/sub/x.cfg", True),
    ("BepInEx/config/**/*.cfg", "BepInEx/config/x.cfg", True),
    ("BepInEx/cache/", "BepInEx/cache/a/b.dat", True),
    ("BepInEx/cache/", "BepInEx/cachex", False),
    ("/mods.yml", "mods.yml", True),
    ("/mods.yml", "sub/mods.yml", False),
    ("mods.yml", "sub/mods.yml", True),
    ("bepinex/CONFIG/*.CFG", "BepInEx/config/x.cfg", True),  # case-insensitive
])
def test_globs(pattern, path, expected):
    assert PathMatcher([pattern]).matches(path) is expected


LIBRARYFOLDERS = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"{root}"
		"label"		""
		"apps"
		{
			"228980"		"123"
		}
	}
	// second drive
	"1"
	{
		"path"		"{lib2}"
		"apps"
		{
			"892970"		"1500000000"
		}
	}
}
'''


def make_steam(tmp_path, native=True):
    root = tmp_path / "steam"
    lib2 = tmp_path / "games"
    (root / "steamapps").mkdir(parents=True)
    write(root, "steamapps/libraryfolders.vdf",
          LIBRARYFOLDERS.replace("{root}", str(root).replace("\\", "\\\\")).replace("{lib2}", str(lib2).replace("\\", "\\\\")))
    write(lib2, "steamapps/appmanifest_892970.acf", '"AppState"\n{\n\t"appid"\t"892970"\n\t"installdir"\t"Valheim"\n}\n')
    write(lib2, "steamapps/common/Valheim/" + ("valheim.x86_64" if native else "valheim.exe"), b"")
    return root, lib2 / "steamapps" / "common" / "Valheim"


def test_find_valheim_in_second_library(tmp_path):
    root, game = make_steam(tmp_path)
    assert steam.find_valheim([root]) == game
    assert steam.steam_root_for_game(game, [root]) == root


def test_old_style_libraryfolders(tmp_path):
    root = tmp_path / "steam"
    lib2 = tmp_path / "lib2"
    write(root, "steamapps/libraryfolders.vdf",
          '"LibraryFolders"\n{\n"TimeNextStatsReport" "1"\n"1" "%s"\n}\n' % str(lib2).replace("\\", "\\\\"))
    write(lib2, "steamapps/common/Valheim/valheim.exe", b"")
    assert steam.find_valheim([root]) == lib2 / "steamapps/common/Valheim"


def test_not_found(tmp_path):
    (tmp_path / "steam" / "steamapps").mkdir(parents=True)
    assert steam.find_valheim([tmp_path / "steam"]) is None


def test_launch_options(tmp_path):
    root, _ = make_steam(tmp_path, native=False)
    write(root, "userdata/1234/config/localconfig.vdf", r'''
"UserLocalConfigStore"
{
  "Software" { "Valve" { "steam" { "apps" {
     "892970" { "LaunchOptions" "WINEDLLOVERRIDES=\"winhttp=n,b\" %command%" }
  } } } }
}''')
    assert steam.launch_options(root) == ['WINEDLLOVERRIDES="winhttp=n,b" %command%']


def test_vdf_errors():
    with pytest.raises(steam.VdfError):
        steam.parse_vdf('"a" { "b" "c"')


def test_linux_root_candidates_include_flatpak(tmp_path):
    flat = tmp_path / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    (flat / "steamapps").mkdir(parents=True)
    assert steam.find_steam_roots("linux", tmp_path) == [flat]
