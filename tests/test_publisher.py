import json

import pytest

from lynxlauncher.publisher import PublishConfig, PublishError, ServerConfig, is_remote, load_config, publish

from .conftest import write

quiet = dict(out=lambda s: None)


def test_publish_writes_manifest_and_objects(do_publish, web_root):
    m = do_publish()
    data = json.loads((web_root / "manifest.json").read_text())
    paths = {f["path"] for f in data["files"]}
    assert "BepInEx/plugins/ModB.dll" in paths
    assert "mods.yml" not in paths and "BepInEx/LogOutput.log" not in paths
    assert data["keep"] == ["BepInEx/config/personal.cfg"]
    for e in m.files:
        assert (web_root / m.object_relpath(e.sha256)).read_bytes() is not None


def test_version_is_content_based(do_publish, gale_profile):
    v1 = do_publish().version
    assert do_publish().version == v1
    write(gale_profile, "BepInEx/plugins/ModB.dll", b"v2")
    assert do_publish().version != v1


def test_dry_run_writes_nothing(do_publish, web_root):
    do_publish(dry_run=True)
    assert list(web_root.iterdir()) == []


def test_prune(do_publish, gale_profile, web_root):
    m1 = do_publish()
    old = next(e for e in m1.files if e.path.endswith("ModB.dll"))
    write(gale_profile, "BepInEx/plugins/ModB.dll", b"v2")
    do_publish()
    assert (web_root / m1.object_relpath(old.sha256)).exists()  # old object kept by default
    do_publish(prune=True)
    assert not (web_root / m1.object_relpath(old.sha256)).exists()


def test_server_push_local(gale_profile, web_root, tmp_path):
    srv = tmp_path / "pelican" / "volume"
    write(srv, "BepInEx/plugins/OldServerMod.dll", b"old")
    write(srv, "BepInEx/plugins/ModB.dll", b"stale")
    cfg = PublishConfig(source=gale_profile, dest=str(web_root),
                        server=ServerConfig(enabled=True, dest=str(srv), exclude=["BepInEx/plugins/ModA/"], existing_only=False))
    publish(cfg, dry_run=True, **quiet)
    assert (srv / "BepInEx/plugins/ModB.dll").read_bytes() == b"stale"  # dry run
    publish(cfg, **quiet)
    assert (srv / "BepInEx/plugins/ModB.dll").read_bytes() == b"mod b v1"
    assert not (srv / "BepInEx/plugins/ModA").exists()  # excluded (client-only)
    assert (srv / "BepInEx/plugins/OldServerMod.dll").exists()  # delete=false by default
    cfg.server.delete = True
    publish(cfg, **quiet)
    assert not (srv / "BepInEx/plugins/OldServerMod.dll").exists()


def test_server_push_skipped_unless_enabled(gale_profile, web_root, tmp_path):
    srv = tmp_path / "srv"
    srv.mkdir()
    cfg = PublishConfig(source=gale_profile, dest=str(web_root), server=ServerConfig(dest=str(srv), existing_only=False))
    publish(cfg, **quiet)
    assert not any(srv.iterdir())
    publish(cfg, push_server=True, **quiet)
    assert (srv / "BepInEx/plugins/ModB.dll").exists()


def test_load_config(tmp_path):
    p = tmp_path / "publish.toml"
    p.write_text('source = "~/gale/P"\ndest = "me@host:/srv/x"\nkeep = ["a.cfg"]\n[server]\nenabled = true\ndest = "/v"\n')
    cfg = load_config(p)
    assert cfg.source.name == "P" and "~" not in str(cfg.source)
    assert cfg.keep == ["a.cfg"] and cfg.server.enabled and cfg.server.paths == ["BepInEx/plugins"]
    p.write_text('dest = "x"\n')
    with pytest.raises(PublishError, match="source"):
        load_config(p)


@pytest.mark.parametrize("dest,remote", [
    ("me@host:/srv/x", True), ("host:/srv", True), ("/srv/x", False), ("C:\\www", False), ("C:/www", False),
])
def test_is_remote(dest, remote):
    assert is_remote(dest) is remote


def test_server_existing_only_never_adds_client_mods(gale_profile, web_root, tmp_path):
    srv = tmp_path / "srv"
    write(srv, "BepInEx/plugins/ModA/ModA.dll", b"old a")              # server mod, out of date
    write(srv, "BepInEx/plugins/Server_devcommands/dc.dll", b"server")  # server-only mod
    cfg = PublishConfig(source=gale_profile, dest=str(web_root),
                        server=ServerConfig(enabled=True, dest=str(srv), delete=True))
    publish(cfg, **quiet)
    assert (srv / "BepInEx/plugins/ModA/ModA.dll").read_bytes() == b"mod a v1"   # updated
    assert not (srv / "BepInEx/plugins/ModB.dll").exists()                      # client mod not added
    assert (srv / "BepInEx/plugins/Server_devcommands/dc.dll").exists()        # protected even with delete
    cfg.server.existing_only = False
    publish(cfg, **quiet)
    assert (srv / "BepInEx/plugins/ModB.dll").exists()


def test_parse_rsync_list():
    from lynxlauncher.publisher import parse_rsync_list
    out = ("drwxr-xr-x          4,096 2026/10/17 18:59:00 .\n"
           "drwxr-xr-x          4,096 2026/10/17 18:59:00 Bryheim-Kvist\n"
           "-rw-r--r--         12,345 2026/10/17 18:59:00 Loose Mod.dll\n")
    assert parse_rsync_list(out) == {"Bryheim-Kvist", "Loose Mod.dll"}


def test_run_logs_everything_but_prints_only_changes(tmp_path, capsys):
    import sys
    from lynxlauncher.publisher import PublishLog, _run
    script = tmp_path / "fake_rsync.py"
    script.write_text(
        "print('.d..t...... BepInEx/plugins/')\n"
        "print('.f..t...... BepInEx/plugins/A/A.dll')\n"
        "print('<fcst...... BepInEx/plugins/LynxInc-LynxModFixes/LynxModFixes.dll')\n"
        "print('cd+++++++++ BepInEx/plugins/New/')\n"
        "print('*deleting   BepInEx/plugins/Old.dll')\n")
    log = PublishLog(tmp_path / "logs")
    n = _run([sys.executable, str(script)], log)
    log.close()
    printed = capsys.readouterr().out
    text = log.path.read_text()
    assert n == 3
    assert "LynxModFixes.dll" in printed and "New/" in printed and "Old.dll" in printed
    assert "A/A.dll" not in printed and "A/A.dll" in text  # attribute-only: log only


def test_publish_log_rotation(tmp_path):
    from lynxlauncher.publisher import PublishLog
    d = tmp_path / "logs"
    d.mkdir()
    for i in range(5):
        (d / f"publish-2020-01-0{i}_000000.log").write_text("old")
    PublishLog(d, keep=3).close()
    assert len(list(d.glob("publish-*.log"))) == 3


def test_main_writes_log_file(gale_profile, web_root, tmp_path, monkeypatch, capsys):
    from lynxlauncher import publisher
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = tmp_path / "p.toml"
    cfg.write_text(f'source = "{gale_profile.as_posix()}"\ndest = "{web_root.as_posix()}"\n')
    assert publisher.main(["-c", str(cfg), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Full log:" in out and "ModB.dll" not in out
    logs = list((tmp_path / "state/lynx-mod-launcher/publish-logs").glob("*.log"))
    assert len(logs) == 1 and "ModB.dll" in logs[0].read_text()
