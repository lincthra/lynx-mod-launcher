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
                        server=ServerConfig(enabled=True, dest=str(srv), exclude=["BepInEx/plugins/ModA/"]))
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
    cfg = PublishConfig(source=gale_profile, dest=str(web_root), server=ServerConfig(dest=str(srv)))
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
