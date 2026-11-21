import json

import pytest

from lynxlauncher.http import HttpFetcher
from lynxlauncher.manifest import Manifest, ManifestError
from lynxlauncher.sync import STATE_DIRNAME, Syncer

from .conftest import write


def files_of(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and STATE_DIRNAME not in p.relative_to(root).parts
    }


@pytest.fixture
def profile(tmp_path):
    return tmp_path / "client" / "profile"


def syncer(profile, **kw):
    return Syncer(profile, HttpFetcher(timeout=5), **kw)


def test_first_sync_installs_everything_except_excludes(do_publish, server, gale_profile, profile):
    m = do_publish()
    r = syncer(profile).run(server)
    assert r.ok and not r.offline
    assert r.updated == len(m.files)
    got = files_of(profile)
    want = {k: v for k, v in files_of(gale_profile).items() if k not in ("BepInEx/LogOutput.log", "mods.yml")}
    assert got == want
    assert r.manifest_version == m.version


def test_second_sync_is_noop_and_uses_hash_cache(do_publish, server, profile, monkeypatch):
    do_publish()
    syncer(profile).run(server)
    import lynxlauncher.sync as sync_mod

    calls = []
    real = sync_mod.sha256_file
    monkeypatch.setattr(sync_mod, "sha256_file", lambda p: calls.append(p) or real(p))
    r = syncer(profile).run(server)
    assert r.ok and r.updated == 0 and r.deleted == 0 and r.up_to_date > 0
    assert calls == []  # nothing rehashed: cache hit on size + mtime
    assert r.headline() == "Up to date"


def test_update_changes_removes_and_keeps(do_publish, server, gale_profile, profile):
    do_publish()
    syncer(profile).run(server)

    # Michael updates the pack
    write(gale_profile, "BepInEx/plugins/ModB.dll", b"mod b v2!")
    for p in (gale_profile / "BepInEx/plugins/ModA").iterdir():
        p.unlink()
    (gale_profile / "BepInEx/plugins/ModA").rmdir()
    write(gale_profile, "BepInEx/plugins/ModC.dll", b"new mod")
    write(gale_profile, "BepInEx/config/personal.cfg", "keybind=F2\n")  # new default, must NOT overwrite
    do_publish()

    # friend's local state
    write(profile, "BepInEx/config/personal.cfg", "keybind=MINE\n")
    write(profile, "BepInEx/plugins/StrayMod.dll", b"not in pack")
    write(profile, "BepInEx/LogOutput.log", "friend log\n")
    write(profile, "BepInEx/plugins/ModB.dll", b"tampered")  # same path, local damage

    r = syncer(profile).run(server)
    assert r.ok, r.error
    got = files_of(profile)
    assert got["BepInEx/plugins/ModB.dll"] == b"mod b v2!"
    assert got["BepInEx/plugins/ModC.dll"] == b"new mod"
    assert "BepInEx/plugins/ModA/ModA.dll" not in got
    assert not (profile / "BepInEx/plugins/ModA").exists()  # empty folder pruned
    assert "BepInEx/plugins/StrayMod.dll" not in got
    assert got["BepInEx/config/personal.cfg"] == b"keybind=MINE\n"
    assert got["BepInEx/LogOutput.log"] == b"friend log\n"
    assert r.updated == 2 and r.deleted == 2
    assert r.headline() == "Updated 2 files, removed 2"


def test_missing_keep_file_is_installed(do_publish, server, profile):
    do_publish()
    syncer(profile).run(server)
    (profile / "BepInEx/config/personal.cfg").unlink()
    r = syncer(profile).run(server)
    assert r.updated == 1
    assert (profile / "BepInEx/config/personal.cfg").read_text() == "keybind=F1\n"


def test_offline_launches_with_what_is_there(do_publish, server, profile):
    do_publish()
    syncer(profile).run(server)
    before = files_of(profile)
    r = syncer(profile).run("http://127.0.0.1:9/manifest.json")
    assert r.offline and not r.ok
    assert files_of(profile) == before
    assert r.headline() == "Couldn't reach the mod server"


def test_http_404_is_reported(server, profile):
    r = syncer(profile).run(server)  # nothing published yet
    assert r.offline and "404" in r.error


def test_corrupt_download_changes_nothing(do_publish, server, gale_profile, web_root, profile):
    do_publish()
    syncer(profile).run(server)
    write(gale_profile, "BepInEx/plugins/ModB.dll", b"mod b v2")
    write(gale_profile, "BepInEx/plugins/ModC.dll", b"new mod")
    m = do_publish()
    entry = next(e for e in m.files if e.path.endswith("ModB.dll"))
    (web_root / m.object_relpath(entry.sha256)).write_bytes(b"evil bytes")
    before = files_of(profile)

    r = syncer(profile).run(server)
    assert not r.ok and "corrupted" in r.error
    assert files_of(profile) == before  # nothing applied, not even ModC
    assert not (profile / STATE_DIRNAME / "staging").exists()


def test_unsafe_manifest_is_rejected(web_root, server, profile, tmp_path):
    bad = {"schema": 1, "name": "x", "version": "1", "files": [
        {"path": "../escape.dll", "size": 1, "sha256": "0" * 64}]}
    (web_root / "manifest.json").write_text(json.dumps(bad))
    r = syncer(profile).run(server)
    assert not r.ok and not r.offline and "unsafe path" in r.error
    assert not (tmp_path / "client" / "escape.dll").exists()


@pytest.mark.parametrize("path", ["/abs.dll", "C:/x.dll", "a\\b.dll", "a/../b", "con.txt", "a/b.", "a//b"])
def test_manifest_path_validation(path):
    with pytest.raises(ManifestError):
        Manifest.from_dict({"schema": 1, "files": [{"path": path, "size": 0, "sha256": "0" * 64}]})


def test_manifest_case_collision_and_file_dir_conflict():
    sha = "a" * 64
    with pytest.raises(ManifestError, match="case"):
        Manifest.from_dict({"schema": 1, "files": [{"path": "A.dll", "size": 0, "sha256": sha},
                                                   {"path": "a.dll", "size": 0, "sha256": sha}]})
    with pytest.raises(ManifestError, match="both a file and a folder"):
        Manifest.from_dict({"schema": 1, "files": [{"path": "a", "size": 0, "sha256": sha},
                                                   {"path": "a/b", "size": 0, "sha256": sha}]})


def test_newer_schema_asks_for_new_launcher(web_root, server, profile):
    (web_root / "manifest.json").write_text(json.dumps({"schema": 99, "files": []}))
    r = syncer(profile).run(server)
    assert "newer Lynx Mod Launcher" in r.error


def test_case_insensitive_client_does_not_churn(do_publish, server, profile):
    do_publish()
    syncer(profile, case_insensitive=True).run(server)
    # A Windows user's folder ended up with different case
    (profile / "BepInEx/plugins/ModB.dll").rename(profile / "BepInEx/plugins/modb.DLL")
    r = syncer(profile, case_insensitive=True).run(server)
    assert r.ok and r.updated == 0 and r.deleted == 0


def test_file_url_works(do_publish, web_root, profile):
    do_publish()
    r = syncer(profile).run((web_root / "manifest.json").as_uri())
    assert r.ok and r.updated > 0


def test_identical_files_share_one_object(gale_profile, do_publish, server, profile, web_root):
    write(gale_profile, "BepInEx/plugins/Copy1.dll", b"same")
    write(gale_profile, "BepInEx/plugins/Copy2.dll", b"same")
    m = do_publish()
    assert len({e.sha256 for e in m.files}) == len(m.files) - 1
    r = syncer(profile).run(server)
    assert r.ok
    assert (profile / "BepInEx/plugins/Copy1.dll").read_bytes() == b"same"
    assert (profile / "BepInEx/plugins/Copy2.dll").read_bytes() == b"same"


def test_gui_throttle_keeps_phase_changes_and_final():
    import time
    from lynxlauncher.sync import Progress
    try:
        from lynxlauncher.gui import throttled
    except ImportError:  # no tkinter on this Python
        pytest.skip("tkinter not available")
    got = []
    f = throttled(got.append, interval=10)
    f(Progress("manifest"))
    for i in range(1000):
        f(Progress("download", i, 1000))
    f(Progress("download", 1000, 1000))
    f(Progress("apply", 0, 5))
    assert [p.phase for p in got] == ["manifest", "download", "download", "apply"]
    assert got[2].done == 1000


def test_second_instance_waits_for_lock(do_publish, server, profile):
    from lynxlauncher.sync import ProfileLock, STATE_DIRNAME
    do_publish()
    with ProfileLock(profile / STATE_DIRNAME):
        r = syncer(profile).run(server, lock_timeout=1)
    assert not r.ok and "already updating" in r.error
    assert syncer(profile).run(server).ok  # free again
