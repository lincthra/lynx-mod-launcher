import os
import stat
import sys
from pathlib import Path

import pytest

from lynxlauncher import publisher
from lynxlauncher.http import HttpFetcher
from lynxlauncher.publisher import PublishConfig, PublishError, load_config, publish
from lynxlauncher.sync import Syncer

from .conftest import write

quiet = dict(out=lambda s: None)


def sync(profile, url):
    return Syncer(profile, HttpFetcher(timeout=5)).run(url)


# --- config files that mods write per character / per world ------------------------

def test_mod_created_config_files_are_left_alone(do_publish, server, gale_profile, tmp_path):
    profile = tmp_path / "client"
    do_publish()
    assert sync(profile, server).ok
    # mods on the friend's PC write their own data into config/ and plugins/
    write(profile, "BepInEx/config/SeneaL-UI/stats/friendchar.dat", b"friend stats")
    write(profile, "BepInEx/config/QuickStackStore_player_123.dat", b"qs")
    write(profile, "BepInEx/plugins/RandomExtra.dll", b"not in pack")
    # Michael drops shared.cfg from the pack
    (gale_profile / "BepInEx/config/shared.cfg").unlink()
    do_publish()
    r = sync(profile, server)
    assert r.ok
    assert (profile / "BepInEx/config/SeneaL-UI/stats/friendchar.dat").read_bytes() == b"friend stats"
    assert (profile / "BepInEx/config/QuickStackStore_player_123.dat").exists()
    assert not (profile / "BepInEx/plugins/RandomExtra.dll").exists()  # plugins/ is mirrored
    assert not (profile / "BepInEx/config/shared.cfg").exists()  # we installed it, it left the pack
    assert r.deleted == 2


def test_default_excludes_logs_and_backups(gale_profile, web_root):
    write(gale_profile, "BepInEx/config/Seasonality/Seasonality-LogOutput.log", "x")
    write(gale_profile, "BepInEx/config/Foo/Bar.yml.old", "x")
    write(gale_profile, "export.r2x", "x")
    m = publish(PublishConfig(source=gale_profile, dest=str(web_root)), **quiet)
    paths = {e.path for e in m.files}
    assert not {p for p in paths if p.endswith((".log", ".old", ".r2x"))}


def test_secrets_block_publishing(gale_profile, web_root):
    write(gale_profile, "BepInEx/config/AutoServerPassword/passwords.json", '{"Password": "x"}')
    cfg = PublishConfig(source=gale_profile, dest=str(web_root))
    with pytest.raises(PublishError, match="passwords.json"):
        publish(cfg, **quiet)
    assert not (web_root / "manifest.json").exists()
    cfg.exclude = ["BepInEx/config/AutoServerPassword/"]
    publish(cfg, **quiet)
    cfg.exclude = []
    cfg.allow_secrets = ["passwords.json"]
    publish(cfg, **quiet)


def test_profile_by_name(tmp_path, monkeypatch):
    gale = tmp_path / "gale-profiles"
    (gale / "Pack1").mkdir(parents=True)
    monkeypatch.setenv("LYNX_GALE_PROFILES", str(gale))
    cfg_file = tmp_path / "publish.toml"
    cfg_file.write_text('profile = "Pack1"\ndest = "github:me/modpack"\n')
    cfg = load_config(cfg_file)
    assert cfg.source == gale / "Pack1" and cfg.name == "Pack1"
    cfg = load_config(cfg_file, profile="Pack2")
    assert cfg.source == gale / "Pack2" and cfg.name == "Pack2"


# --- GitHub Releases storage (with a fake gh) -------------------------------------

@pytest.fixture
def fake_gh(tmp_path, monkeypatch, web_root, server):
    if os.name == "nt":
        pytest.skip("fake gh is a POSIX script")
    script = tmp_path / "gh"
    script.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_gh.py").read_text())
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LYNX_GH", str(script))
    monkeypatch.setenv("FAKE_GH_ROOT", str(web_root))
    base = server.rsplit("/", 1)[0]  # http://127.0.0.1:port
    monkeypatch.setattr(publisher, "github_manifest_url", lambda repo: f"{base}/{repo}/releases/download/manifest/manifest.json")
    monkeypatch.setattr(publisher, "github_objects_template", lambda repo: f"{base}/{repo}/releases/download/objects-{{sha1}}/{{sha}}")
    return web_root


def test_github_publish_and_sync(fake_gh, gale_profile, tmp_path):
    cfg = PublishConfig(source=gale_profile, dest="github:lynx/modpack")
    m = publish(cfg, **quiet)
    rel = fake_gh / "lynx/modpack/releases/download"
    assert (rel / "manifest/manifest.json").is_file()
    for e in m.files:
        assert (rel / f"objects-{e.sha256[0]}" / e.sha256).is_file()

    profile = tmp_path / "client"
    url = publisher.github_manifest_url("lynx/modpack")
    r = sync(profile, url)
    assert r.ok and r.updated == len(m.files)
    assert (profile / "BepInEx/plugins/ModB.dll").read_bytes() == b"mod b v1"

    # second publish uploads only what changed
    write(gale_profile, "BepInEx/plugins/ModB.dll", b"mod b v2")
    calls = fake_gh / "calls.log"
    calls.write_text("")
    publish(cfg, **quiet)
    uploads = [line for line in calls.read_text().splitlines() if line.startswith("release upload")]
    assert len(uploads) == 2  # the new ModB object + the manifest
    r = sync(profile, url)
    assert r.updated == 1 and (profile / "BepInEx/plugins/ModB.dll").read_bytes() == b"mod b v2"

    old = next(e for e in m.files if e.path.endswith("ModB.dll"))
    publish(cfg, prune=True, **quiet)
    assert not (rel / f"objects-{old.sha256[0]}" / old.sha256).exists()


def test_github_empty_repo_hint(fake_gh, gale_profile, monkeypatch):
    monkeypatch.setenv("FAKE_GH_EMPTY_REPO", "1")
    with pytest.raises(PublishError, match="at least one commit"):
        publish(PublishConfig(source=gale_profile, dest="github:lynx/empty"), **quiet)


def test_github_dest_is_not_rsync():
    assert publisher.github_repo("github:lynx/modpack") == "lynx/modpack"
    assert not publisher.is_remote("github:lynx/modpack")


def test_plugin_binaries_are_not_flagged_as_secrets(gale_profile, web_root):
    write(gale_profile, "BepInEx/plugins/Allifreyr-AutoServerPassword/AutoServerPassword.dll", b"dll")
    write(gale_profile, "BepInEx/plugins/warpalicious-More_World_Locations_AIO/Bundles/mwl_secretroom1", b"bundle")
    publish(PublishConfig(source=gale_profile, dest=str(web_root)), **quiet)


def test_missing_dest_folder_fails_even_on_dry_run(gale_profile, tmp_path):
    cfg = PublishConfig(source=gale_profile, dest=str(tmp_path / "typo"))
    for dry in (True, False):
        with pytest.raises(PublishError, match="doesn't exist"):
            publish(cfg, dry_run=dry, **quiet)
    assert not (tmp_path / "typo").exists()


def test_main_says_nothing_was_published(gale_profile, tmp_path, capsys):
    cfg = tmp_path / "p.toml"
    cfg.write_text(f'source = "{gale_profile.as_posix()}"\ndest = "{(tmp_path / "nope").as_posix()}"\n')
    assert publisher.main(["-c", str(cfg)]) == 1
    assert "Nothing was published" in capsys.readouterr().err


def test_empty_files_are_not_uploaded_but_still_installed(fake_gh, gale_profile, tmp_path):
    write(gale_profile, "BepInEx/config/alias.yaml", b"")
    write(gale_profile, "BepInEx/plugins/Odinrealm-OdinRealmFishing/.gitkeep", b"")
    cfg = PublishConfig(source=gale_profile, dest="github:lynx/modpack")
    publish(cfg, **quiet)
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert not (fake_gh / "lynx/modpack/releases/download/objects-e" / empty).exists()
    profile = tmp_path / "client"
    r = sync(profile, publisher.github_manifest_url("lynx/modpack"))
    assert r.ok, r.error
    assert (profile / "BepInEx/config/alias.yaml").read_bytes() == b""
    assert (profile / "BepInEx/plugins/Odinrealm-OdinRealmFishing/.gitkeep").exists()
    assert sync(profile, publisher.github_manifest_url("lynx/modpack")).updated == 0
