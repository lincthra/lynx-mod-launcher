import os
import sys
import time

import pytest

from lynxlauncher import doorstop, steam
from lynxlauncher.launcher_app import LauncherCore

from .conftest import write

REGISTRY = '''"Registry"
{
	"HKCU"
	{
		"Software"
		{
			"Valve"
			{
				"Steam"
				{
					"ActiveUser"		"%s"
					"pid"		"1234"
				}
			}
		}
	}
}
'''


def fake_proc(tmp_path, *names):
    proc = tmp_path / "proc"
    proc.mkdir(parents=True, exist_ok=True)
    write(proc, "self/comm", "python\n")
    for i, n in enumerate(names, start=100):
        write(proc, f"{i}/comm", n + "\n")
    return proc


def test_steam_ready_needs_process_and_login(tmp_path):
    home = tmp_path / "home"
    write(home, ".steam/registry.vdf", REGISTRY % "12345678")
    assert steam.steam_ready(home, fake_proc(tmp_path, "bash", "steamwebhelper"))
    assert not steam.steam_ready(home, fake_proc(tmp_path / "x", "bash"))  # not running
    write(home, ".steam/registry.vdf", REGISTRY % "0")
    assert not steam.steam_ready(home, fake_proc(tmp_path, "steam"))  # at the login screen


def test_flatpak_registry_and_stale_login(tmp_path):
    home = tmp_path / "home"
    reg = write(home, ".var/app/com.valvesoftware.Steam/.steam/registry.vdf", REGISTRY % "42")
    assert steam.steam_logged_in(home)
    old = time.time() - 600
    os.utime(reg, (old, old))
    assert not steam.steam_logged_in(home, newer_than=time.time() - 5)  # left over from a crash


class FakeSteam:
    def __init__(self, ready_after=None, running=False):
        self.ready_after = ready_after  # seconds after start_steam
        self.running = running
        self.started_at = None
        self.t = 0.0

    def install(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(steam, "steam_process_running", lambda *a, **k: self.running)
        monkeypatch.setattr(steam, "steam_ready", self.ready)
        monkeypatch.setattr(steam, "start_steam", self.start)

    def start(self, cmd):
        assert cmd == ["steam"]
        self.started_at = self.t
        self.running = True

    def ready(self, *a, newer_than=0.0, **k):
        return self.started_at is not None and self.ready_after is not None and self.t - self.started_at >= self.ready_after

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t


def core(tmp_path):
    return LauncherCore("http://x", data_dir=tmp_path / "data")


def native_spec():
    return doorstop.LaunchSpec(["valheim.x86_64"], kind=doorstop.GameKind.LINUX_NATIVE, start_steam=["steam"])


def test_starts_steam_and_waits_for_login(tmp_path, monkeypatch):
    fs = FakeSteam(ready_after=20)
    fs.install(monkeypatch)
    msgs = []
    core(tmp_path).ensure_steam(native_spec(), msgs.append, sleep=fs.sleep, clock=fs.clock)
    assert fs.started_at == 0 and msgs[0] == "Starting Steam…" and msgs[-1].startswith("Steam is ready")
    assert fs.t >= 25  # waited for login, then a short grace period


def test_gives_up_if_steam_never_signs_in(tmp_path, monkeypatch):
    fs = FakeSteam(ready_after=None)
    fs.install(monkeypatch)
    with pytest.raises(doorstop.DoorstopError, match="sign in"):
        core(tmp_path).ensure_steam(native_spec(), sleep=fs.sleep, clock=fs.clock, timeout=30)


def test_nothing_to_do_for_proton_or_when_ready(tmp_path, monkeypatch):
    fs = FakeSteam()
    fs.install(monkeypatch)
    spec = doorstop.LaunchSpec(["steam", "-applaunch"], kind=doorstop.GameKind.LINUX_PROTON)
    core(tmp_path).ensure_steam(spec, sleep=fs.sleep, clock=fs.clock)
    assert fs.started_at is None
    monkeypatch.setattr(steam, "steam_ready", lambda *a, **k: True)
    core(tmp_path).ensure_steam(native_spec(), sleep=fs.sleep, clock=fs.clock)
    assert fs.started_at is None and fs.t == 0


LOG_ON = """[2026-11-22 12:18:09] [Logged Off, 0, 0] [U:1:1] Log session ended
[2026-11-22 12:19:36] [Logging On, 4, 7] [U:1:1] RecvMsgClientLogOnResponse() : [U:1:1] 'OK'
[2026-11-22 12:19:36] [Logged On, 4, 7] [U:1:1] RecvMsgClientLogOnResponse() : processing complete
[2026-11-22 12:19:36] CClientJobGetClientUpdateHosts: cached version not expired
"""
LOG_OFF = """[2026-11-22 12:18:08] [Logged Off, 0, 0] [U:1:1] ConnectionDisconnected() not auto reconnecting due to user initiated logoff
[2026-11-22 12:18:09] [Logged Off, 0, 0] [U:1:1] Log session ended
"""


def test_connection_log_decides_login(tmp_path):
    home = tmp_path / "home"
    log = write(home, ".local/share/Steam/logs/connection_log.txt", LOG_OFF)
    assert not steam.steam_logged_in(home)
    log.write_text(LOG_OFF + LOG_ON)
    assert steam.steam_logged_in(home)
    proc = fake_proc(tmp_path, "steam")
    assert steam.steam_ready(home, proc)
    old = time.time() - 600
    os.utime(log, (old, old))
    assert not steam.steam_logged_in(home, newer_than=time.time() - 5)  # stale: from before we started Steam
