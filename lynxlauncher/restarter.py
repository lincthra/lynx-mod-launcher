"""Restart the Valheim server once nobody is on it.

After a server update Michael usually waits until people are off before restarting. This does
the waiting: every few minutes it asks the server how many players are on (the Steam server
query, which Valheim answers on the game port + 1), and once it has seen 0 players on
`empty_checks` checks in a row it asks Pelican to restart the server (the same as the panel's
Restart button, so the egg's normal stop runs and the world is saved).

It never starts on its own: lynx-publish asks first. It runs as a detached background process,
writes its progress to a log, and can be checked or cancelled with lynx-publish
--restart-status / --restart-cancel. It gives up after `give_up_hours`.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

A2S_INFO = b"\xff\xff\xff\xffTSource Engine Query\x00"


class RestartError(Exception):
    pass


@dataclass
class RestartConfig:
    panel: str                 # https://pelican.example.com
    server_id: str             # the short id in the panel URL: /server/<id>
    query_host: str
    query_port: int = 2457     # Valheim: game port + 1
    api_key_file: str = ""
    poll_minutes: float = 5
    empty_checks: int = 2
    give_up_hours: float = 24

    def api_key(self) -> str:
        key = os.environ.get("LYNX_PELICAN_KEY", "").strip()
        if key:
            return key
        path = Path(os.path.expandvars(self.api_key_file or str(default_key_path()))).expanduser()
        try:
            key = path.read_text("utf-8").strip()
        except FileNotFoundError:
            raise RestartError(f"no Pelican API key: put a client API key (Account > API Credentials) in {path}") from None
        if not key:
            raise RestartError(f"{path} is empty")
        return key


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lynx-mod-launcher"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "lynx-mod-launcher"


def default_key_path() -> Path:
    return config_dir() / "pelican.key"


def job_path() -> Path:
    return state_dir() / "restart-job.json"


def job_log_path() -> Path:
    return state_dir() / "restart-when-empty.log"


# -- talking to the game server and the panel ------------------------------------


def query_players(host: str, port: int, timeout: float = 5) -> tuple[int, int, str]:
    """(players, max_players, server name) via the Steam A2S_INFO query."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        addr = (host, port)
        s.sendto(A2S_INFO, addr)
        data = s.recv(1400)
        if len(data) >= 9 and data[4] == 0x41:  # challenge: repeat with it appended
            s.sendto(A2S_INFO + data[5:9], addr)
            data = s.recv(1400)
    if len(data) < 6 or data[4] != 0x49:
        raise RestartError(f"unexpected reply from {host}:{port}")
    p = data[6:]
    strings = []
    for _ in range(4):  # name, map, folder, game
        i = p.index(0)
        strings.append(p[:i].decode("utf-8", "replace"))
        p = p[i + 1:]
    if len(p) < 4:
        raise RestartError(f"short reply from {host}:{port}")
    return p[2], p[3], strings[0]  # after the 2-byte app id: players, max players


class Pelican:
    def __init__(self, panel: str, server_id: str, key: str, timeout: float = 20):
        self.base = f"{panel.rstrip('/')}/api/client/servers/{server_id}"
        self.key = key
        self.timeout = timeout

    def _request(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method="POST" if body is not None else "GET",
            headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": "lynx-mod-launcher"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ssl.create_default_context()) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            hint = " (check the API key)" if e.code in (401, 403) else " (check panel and server_id)" if e.code == 404 else ""
            raise RestartError(f"Pelican said HTTP {e.code}{hint}") from e
        except (urllib.error.URLError, OSError) as e:
            raise RestartError(f"couldn't reach Pelican: {e}") from e
        return json.loads(raw) if raw.strip() else {}

    def state(self) -> str:
        """'running', 'starting', 'stopping' or 'offline'."""
        return str(self._request("/resources").get("attributes", {}).get("current_state", "unknown"))

    def restart(self) -> None:
        self._request("/power", {"signal": "restart"})


# -- the waiting loop --------------------------------------------------------------


def wait_and_restart(
    cfg: RestartConfig,
    out: Callable[[str], None],
    *,
    pelican: Pelican | None = None,
    query: Callable[[str, int], tuple[int, int, str]] = query_players,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> bool:
    """Returns True if the server was restarted."""
    pelican = pelican or Pelican(cfg.panel, cfg.server_id, cfg.api_key())
    deadline = now() + cfg.give_up_hours * 3600
    empty = 0
    failures = 0
    out(f"Waiting for {cfg.query_host}:{cfg.query_port} to be empty "
        f"(checking every {cfg.poll_minutes:g} min, restart after {cfg.empty_checks} empty checks in a row).")
    while True:
        try:
            state = pelican.state()
        except RestartError as e:
            state = "unknown"
            out(f"  panel: {e}")
        if state in ("offline", "stopping"):
            out(f"The server is {state}; nothing to restart. It will load the new mods when it starts.")
            return False
        try:
            players, maxp, _name = query(cfg.query_host, cfg.query_port)
            failures = 0
        except (OSError, RestartError, ValueError) as e:
            players, failures = None, failures + 1
            out(f"  couldn't ask the server for players ({e}); not counting this check")
            if failures >= 6:
                out("The server hasn't answered 6 times in a row; giving up without restarting.")
                return False
        if players is not None:
            empty = empty + 1 if players == 0 else 0
            out(f"  {datetime.now():%H:%M}  {players}/{maxp} online" + (f"  (empty {empty}/{cfg.empty_checks})" if players == 0 else ""))
            if empty >= cfg.empty_checks:
                pelican.restart()
                out("Nobody's on: restart sent to Pelican.")
                return True
        if now() + cfg.poll_minutes * 60 > deadline:
            out(f"Still not empty after {cfg.give_up_hours:g} hours; giving up. Restart it from the panel when it suits you.")
            return False
        sleep(cfg.poll_minutes * 60)


# -- running it in the background ---------------------------------------------------


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would *terminate* the process on Windows
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():  # Linux: make sure the pid wasn't reused by something else
        try:
            return b"--restart-wait" in cmdline.read_bytes()
        except OSError:
            return True
    return True


def current_job() -> dict | None:
    try:
        job = json.loads(job_path().read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(job, dict) or not _alive(int(job.get("pid", 0) or 0)):
        return None
    return job


def start_background(config_file: Path, profile: str | None = None) -> int:
    """Start `lynx-publish --restart-wait` detached from this terminal. Returns its pid."""
    if current_job():
        raise RestartError("a restart is already waiting (lynx-publish --restart-status)")
    state_dir().mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "lynxlauncher.publisher", "--restart-wait", "-c", str(config_file)]
    if profile:
        cmd += ["-p", profile]
    env = dict(os.environ)
    pkg_parent = str(Path(__file__).resolve().parent.parent)  # works for pipx installs and a source checkout
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    logf = open(job_log_path(), "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                            start_new_session=True, close_fds=True, env=env, cwd=str(Path.home()))
    logf.close()
    job_path().write_text(json.dumps({"pid": proc.pid, "started": datetime.now().isoformat(timespec="seconds")}), "utf-8")
    return proc.pid


def preflight(cfg: RestartConfig) -> str:
    """Check the key, the panel and the player query before going into the background."""
    state = Pelican(cfg.panel, cfg.server_id, cfg.api_key()).state()
    try:
        players, maxp, name = query_players(cfg.query_host, cfg.query_port)
    except (OSError, ValueError) as e:
        raise RestartError(f"the server didn't answer the player query on {cfg.query_host}:{cfg.query_port} ({e})") from e
    return f"{name}: {state}, {players}/{maxp} online"


def status_text() -> str:
    job = current_job()
    tail = ""
    try:
        lines = job_log_path().read_text("utf-8").strip().splitlines()
        tail = "\n".join(lines[-6:])
    except OSError:
        pass
    if job:
        head = f"A restart is waiting (started {job.get('started', '?')}, pid {job['pid']})."
    else:
        head = "No restart is waiting."
    return head + (f"\nLast lines of {job_log_path()}:\n{tail}" if tail else "")


def cancel() -> bool:
    job = current_job()
    if not job:
        return False
    os.kill(int(job["pid"]), signal.SIGTERM)
    job_path().unlink(missing_ok=True)
    return True
