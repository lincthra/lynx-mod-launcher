import socket
import threading

import pytest

from lynxlauncher import publisher
from lynxlauncher.restarter import RestartConfig, RestartError, query_players, wait_and_restart


def a2s_reply(players, maxp, name="Pridelands"):
    body = b"\x11" + name.encode() + b"\x00" + b"map\x00" + b"valheim\x00" + b"Valheim\x00"
    body += b"\x30\x6b" + bytes([players, maxp, 0]) + b"dl\x00\x00"
    return b"\xff\xff\xff\xff\x49" + body


def fake_server(challenge: bool):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def serve():
        data, addr = s.recvfrom(1400)
        if challenge:
            s.sendto(b"\xff\xff\xff\xff\x41ABCD", addr)
            data, addr = s.recvfrom(1400)
            assert data.endswith(b"ABCD")
        s.sendto(a2s_reply(3, 10), addr)
        s.close()

    threading.Thread(target=serve, daemon=True).start()
    return s.getsockname()[1]


@pytest.mark.parametrize("challenge", [False, True])
def test_query_players(challenge):
    port = fake_server(challenge)
    assert query_players("127.0.0.1", port, timeout=3) == (3, 10, "Pridelands")


class FakePelican:
    def __init__(self, state="running"):
        self.state_value = state
        self.restarts = 0

    def state(self):
        return self.state_value

    def restart(self):
        self.restarts += 1


def run_loop(counts, state="running", **cfg_kw):
    cfg = RestartConfig(panel="https://p", server_id="abc", query_host="h", **cfg_kw)
    seq = iter(counts)
    clock = {"t": 0.0}

    def query(_h, _p):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v, 10, "Pridelands"

    def sleep(sec):
        clock["t"] += sec

    pel = FakePelican(state)
    lines = []
    result = wait_and_restart(cfg, lines.append, pelican=pel, query=query, sleep=sleep, now=lambda: clock["t"])
    return result, pel.restarts, lines, clock["t"]


def test_restarts_only_after_two_empty_checks_in_a_row():
    result, restarts, lines, t = run_loop([2, 0, 1, 0, 0])
    assert result is True and restarts == 1
    assert t == 4 * 300  # five checks, four waits of 5 minutes


def test_query_hiccup_does_not_count_as_empty():
    result, restarts, _, _ = run_loop([0, OSError("timeout"), 3, 0, 0])
    assert result and restarts == 1


def test_offline_server_is_left_alone():
    result, restarts, lines, _ = run_loop([0, 0], state="offline")
    assert result is False and restarts == 0 and "nothing to restart" in lines[-1]


def test_gives_up_after_deadline():
    result, restarts, lines, _ = run_loop([1] * 100, give_up_hours=1)
    assert result is False and restarts == 0 and "giving up" in lines[-1]


def test_gives_up_when_server_stops_answering():
    result, restarts, _, _ = run_loop([OSError("x")] * 6)
    assert result is False and restarts == 0


def test_config_parsing(tmp_path):
    cfg_file = tmp_path / "publish.toml"
    cfg_file.write_text(
        'source = "/tmp/p"\ndest = "/tmp/d"\n'
        '[server]\ndest = "lincthra@10.0.0.99:/var/lib/pelican/volumes/x"\n'
        '[server.restart]\npanel = "https://pelican.example"\nserver_id = "ca12ff25"\n'
    )
    rc = publisher.load_config(cfg_file).server.restart
    assert (rc.query_host, rc.query_port, rc.poll_minutes, rc.empty_checks) == ("10.0.0.99", 2457, 5, 2)

    cfg_file.write_text('source = "/tmp/p"\ndest = "/tmp/d"\n[server.restart]\nserver_id = "x"\n')
    with pytest.raises(publisher.PublishError, match="panel"):
        publisher.load_config(cfg_file)


def test_api_key_from_file_or_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LYNX_PELICAN_KEY", raising=False)
    cfg = RestartConfig(panel="p", server_id="s", query_host="h", api_key_file=str(tmp_path / "k"))
    with pytest.raises(RestartError, match="API key"):
        cfg.api_key()
    (tmp_path / "k").write_text("ptlc_abc\n")
    assert cfg.api_key() == "ptlc_abc"
    monkeypatch.setenv("LYNX_PELICAN_KEY", "ptlc_env")
    assert cfg.api_key() == "ptlc_env"


def test_background_job_status_and_cancel(tmp_path, monkeypatch):
    import time as _time

    from lynxlauncher import restarter

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LYNX_PELICAN_KEY", "ptlc_test")
    cfg_file = tmp_path / "publish.toml"
    cfg_file.write_text(
        'source = "/tmp/p"\ndest = "/tmp/d"\n[server]\ndest = "/tmp/srv"\n'
        '[server.restart]\npanel = "http://127.0.0.1:9"\nserver_id = "x"\nquery = "127.0.0.1:9"\npoll_minutes = 1\n'
    )
    pid = restarter.start_background(cfg_file)
    try:
        for _ in range(50):
            if restarter.job_log_path().exists() and "Waiting for" in restarter.job_log_path().read_text():
                break
            _time.sleep(0.1)
        assert restarter.current_job()["pid"] == pid
        assert "A restart is waiting" in restarter.status_text()
        with pytest.raises(RestartError, match="already waiting"):
            restarter.start_background(cfg_file)
        assert restarter.cancel()
        for _ in range(50):
            if not restarter._alive(pid):
                break
            _time.sleep(0.1)
        assert restarter.current_job() is None
        assert "No restart is waiting" in restarter.status_text()
    finally:
        try:
            import os, signal
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_pelican_client_against_fake_panel():
    import json as _json
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from lynxlauncher.restarter import Pelican

    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.append(("GET", self.path, self.headers["Authorization"]))
            self._reply(200, _json.dumps({"object": "stats", "attributes": {"current_state": "running"}}).encode())

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(("POST", self.path, _json.loads(body)))
            self._reply(204)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = Pelican(f"http://127.0.0.1:{srv.server_port}/", "ca12ff25", "ptlc_k")
        assert p.state() == "running"
        p.restart()
    finally:
        srv.shutdown()
    assert seen == [("GET", "/api/client/servers/ca12ff25/resources", "Bearer ptlc_k"),
                    ("POST", "/api/client/servers/ca12ff25/power", {"signal": "restart"})]
