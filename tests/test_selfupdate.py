import hashlib
import json

import pytest

from lynxlauncher import selfupdate
from lynxlauncher.http import HttpFetcher
from lynxlauncher.launcher_app import LauncherCore


def make_release(web_root, version, win=b"new windows exe", linux=b"new linux exe"):
    for name, data in (("LynxModLauncher.exe", win), ("LynxModLauncher-linux", linux)):
        (web_root / name).write_bytes(data)
    assets = {
        "windows": {"url": "LynxModLauncher.exe", "sha256": hashlib.sha256(win).hexdigest(), "size": len(win)},
        "linux": {"url": "LynxModLauncher-linux", "sha256": hashlib.sha256(linux).hexdigest(), "size": len(linux)},
    }
    (web_root / "latest.json").write_text(json.dumps({"version": version, "assets": assets}))


@pytest.fixture
def update_url(server):
    return server.replace("manifest.json", "latest.json")


def test_versions():
    assert selfupdate.parse_version("v0.10.0") > selfupdate.parse_version("0.9.9")
    assert selfupdate.parse_version("0.2.0-beta") == (0, 2, 0)


def test_check_newer_and_same(web_root, update_url):
    make_release(web_root, "0.2.0")
    f = HttpFetcher(timeout=5)
    info = selfupdate.check(update_url, "0.1.0", f, platform="linux")
    assert info and info.version == "0.2.0" and info.url.endswith("/LynxModLauncher-linux")
    assert selfupdate.check(update_url, "0.2.0", f) is None
    assert selfupdate.check(update_url, "0.3.0", f) is None


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_apply_swaps_exe(web_root, update_url, tmp_path, platform):
    make_release(web_root, "0.2.0")
    exe = tmp_path / "app" / "LynxModLauncher.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"old exe")
    f = HttpFetcher(timeout=5)
    info = selfupdate.check(update_url, "0.1.0", f, platform=platform)
    seen = []
    selfupdate.apply(info, f, exe, lambda d, t: seen.append((d, t)), platform=platform)
    expected = b"new windows exe" if platform == "win32" else b"new linux exe"
    assert exe.read_bytes() == expected and seen[-1][0] == len(expected)
    if platform == "win32":
        assert exe.with_name(exe.name + ".old").read_bytes() == b"old exe"
        selfupdate.cleanup_old(exe)
        assert not exe.with_name(exe.name + ".old").exists()


def test_bad_hash_keeps_old_exe(web_root, update_url, tmp_path):
    make_release(web_root, "0.2.0")
    (web_root / "LynxModLauncher-linux").write_bytes(b"tampered!!!!!")  # same length, different bytes
    exe = tmp_path / "LynxModLauncher"
    exe.write_bytes(b"old exe")
    f = HttpFetcher(timeout=5)
    info = selfupdate.check(update_url, "0.1.0", f, platform="linux")
    with pytest.raises(selfupdate.UpdateError):
        selfupdate.apply(info, f, exe, platform="linux")
    assert exe.read_bytes() == b"old exe"
    assert not exe.with_name(exe.name + ".new").exists()


def test_core_check_is_quiet_when_offline_or_not_frozen(tmp_path, monkeypatch):
    core = LauncherCore("http://127.0.0.1:9/m.json", data_dir=tmp_path, update_url="http://127.0.0.1:9/latest.json")
    assert core.check_self_update() is None  # not frozen (running from source)
    monkeypatch.setattr(selfupdate, "is_frozen", lambda: True)
    assert core.check_self_update() is None  # offline: logged, not raised
    core.settings.auto_update = "no"
    assert not core.self_update_enabled()
