import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lynxlauncher.publisher import PublishConfig, publish


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def web_root(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    return root


@pytest.fixture
def server(web_root):
    handler = functools.partial(QuietHandler, directory=str(web_root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/manifest.json"
    httpd.shutdown()
    httpd.server_close()


def write(root: Path, rel: str, data: bytes | str) -> Path:
    p = root.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    return p


@pytest.fixture
def gale_profile(tmp_path):
    """A small fake Gale profile laid out like BepInExPack_Valheim + a few mods."""
    root = tmp_path / "gale" / "TestPack"
    write(root, "BepInEx/core/BepInEx.Preloader.dll", b"preloader")
    write(root, "BepInEx/core/BepInEx.dll", b"bepinex" * 1000)
    write(root, "BepInEx/plugins/ModA/ModA.dll", b"mod a v1")
    write(root, "BepInEx/plugins/ModB.dll", b"mod b v1")
    write(root, "BepInEx/config/shared.cfg", "shared=1\n")
    write(root, "BepInEx/config/personal.cfg", "keybind=F1\n")
    write(root, "winhttp.dll", b"winhttp v4")
    write(root, "doorstop_config.ini", "[General]\nenabled = true\ntarget_assembly = BepInEx\\core\\BepInEx.Preloader.dll\n"
                                       "[UnityMono]\ndll_search_path_override = unstripped_corlib\n")
    write(root, ".doorstop_version", "4.3.0.0\n")
    write(root, "unstripped_corlib/mscorlib.dll", b"corlib")
    write(root, "doorstop_libs/libdoorstop.so", b"\x7fELF")
    write(root, "BepInEx/LogOutput.log", "log line\n")
    write(root, "mods.yml", "gale: metadata\n")
    return root


@pytest.fixture
def publish_cfg(gale_profile, web_root):
    return PublishConfig(source=gale_profile, dest=str(web_root), name="Test",
                         keep=["BepInEx/config/personal.cfg"])


@pytest.fixture
def do_publish(publish_cfg):
    return lambda **kw: publish(publish_cfg, out=lambda s: None, **kw)
