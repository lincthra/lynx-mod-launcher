import json
import sqlite3
from urllib.error import HTTPError

from lynxlauncher.modupdates import check, installed_mods, is_newer, summary

from .conftest import write


def make_gale_db(path, profile_dir, mods):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE profiles (id INTEGER PRIMARY KEY, name TEXT, path TEXT, game_slug TEXT, mods JSON)")
    con.execute("INSERT INTO profiles (name, path, game_slug, mods) VALUES (?, ?, 'valheim', ?)",
                ("Other", "/nowhere/Other", json.dumps([])))
    con.execute("INSERT INTO profiles (name, path, game_slug, mods) VALUES (?, ?, 'valheim', ?)",
                (profile_dir.name, str(profile_dir), json.dumps(mods)))
    con.commit()
    con.close()


GALE_MODS = [
    {"enabled": True, "fullName": "ValheimModding-Jotunn-2.30.2", "packageUuid": "a", "versionUuid": "b",
     "backend": "Thunderstore"},
    {"enabled": True, "fullName": "treextr-PathOfValheiman-6.0.15", "packageUuid": "c", "versionUuid": "d",
     "backend": "Hexium"},
    {"enabled": True, "fullName": "Ross-RossItemDrawers-1.3.0", "packageUuid": "e", "versionUuid": "f"},  # no backend key
    {"enabled": False, "fullName": "Old-DisabledMod-1.0.0", "packageUuid": "g", "versionUuid": "h"},
    {"enabled": True, "fullName": "denikson-BepInExPack_Valheim-5.4.2360", "packageUuid": "i", "versionUuid": "j"},
    # imported from a zip: no author in Gale, folder name supplies it
    {"enabled": True, "name": "ChaosPolymorph", "version": "1.3.0", "uuid": "k"},
]

SITES = {
    "https://thunderstore.io/api/experimental/package/ValheimModding/Jotunn/": "2.31.0",
    "https://hexium.gg/api/experimental/package/treextr/PathOfValheiman/": "6.0.15",  # current
    "https://thunderstore.io/api/experimental/package/treextr/PathOfValheiman/": "9.9.9",  # wrong site: ignored
    "https://thunderstore.io/api/experimental/package/Ross/RossItemDrawers/": "1.2.9",  # older: not an update
    "https://thunderstore.io/api/experimental/package/LynxInc/ChaosPolymorph/": "1.3.1",
    "https://hexium.gg/api/experimental/package/LynxInc/ChaosPolymorph/": "1.4.0",  # newest wins
}


def fake_get(url):
    if url not in SITES:
        raise HTTPError(url, 404, "Not Found", None, None)
    return json.dumps({"latest": {"version_number": SITES[url]}}).encode()


def test_reads_gale_db_and_finds_updates_on_the_right_site(tmp_path):
    profile = tmp_path / "profiles" / "PridelandsPoV"
    write(profile, "BepInEx/plugins/LynxInc-ChaosPolymorph/manifest.json",
          json.dumps({"name": "ChaosPolymorph", "version_number": "1.3.0"}))
    db = tmp_path / "data.sqlite3"
    make_gale_db(db, profile, GALE_MODS)

    mods = installed_mods(profile, db)
    idents = {m.ident for m in mods}
    assert "Old-DisabledMod" not in idents and "denikson-BepInExPack_Valheim" not in idents
    assert "LynxInc-ChaosPolymorph" in idents

    ups = check(mods, fake_get)
    assert [(u.mod.name, u.latest, u.backend) for u in ups] == [
        ("ChaosPolymorph", "1.4.0", "hexium"),
        ("Jotunn", "2.31.0", "thunderstore"),
    ]
    assert ups[0].url == "https://valheim.hexium.gg/mods/LynxInc/ChaosPolymorph"
    assert summary(ups) == "2 mod updates available in Gale: ChaosPolymorph, Jotunn"


def test_falls_back_to_plugin_folders_without_gale_db(tmp_path):
    profile = tmp_path / "PridelandsPoV"
    write(profile, "BepInEx/plugins/ValheimModding-Jotunn/manifest.json",
          json.dumps({"name": "Jotunn", "version_number": "2.30.2"}))
    write(profile, "BepInEx/plugins/loose.dll", b"x")
    mods = installed_mods(profile, tmp_path / "missing.sqlite3")
    assert [(m.ident, m.version, m.backend) for m in mods] == [("ValheimModding-Jotunn", "2.30.2", None)]
    assert [u.latest for u in check(mods, fake_get)] == ["2.31.0"]


def test_offline_means_no_updates_not_an_error(tmp_path):
    profile = tmp_path / "P"
    write(profile, "BepInEx/plugins/ValheimModding-Jotunn/manifest.json", json.dumps({"version_number": "1.0.0"}))

    def offline(url):
        raise OSError("network unreachable")

    assert check(installed_mods(profile, tmp_path / "none"), offline) == []


def test_version_compare():
    assert is_newer("1.10.0", "1.9.9")
    assert not is_newer("1.0", "1.0.0")
    assert not is_newer("1.2.9", "1.3.0")
