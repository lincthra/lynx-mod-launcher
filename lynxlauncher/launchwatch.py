"""Follow BepInEx's LogOutput.log after Launch, so the splash screen can show real progress.

BepInEx writes <profile>/BepInEx/LogOutput.log (it truncates it on each start). We only trust
the file once its mtime is after the moment we clicked Launch, then read it incrementally:
  "Loading [Some Mod 1.2.3]"        -> one more mod loading
  "Chainloader startup complete"    -> all mods loaded; Valheim's window is about to appear
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_LOADING = re.compile(r"Loading \[(?P<name>.+?)\]\s*$")
_READY = "Chainloader startup complete"
_STARTED = ("Preloader started", "Chainloader ready", "BepInEx ")


@dataclass
class LaunchState:
    phase: str = "waiting"  # waiting -> starting -> loading -> ready
    loaded: int = 0
    last_mod: str = ""
    errors: list[str] = field(default_factory=list)


class LaunchWatcher:
    def __init__(self, log_path: Path, started_at: float | None = None):
        self.log_path = Path(log_path)
        self.started_at = time.time() if started_at is None else started_at
        self.state = LaunchState()
        self._offset = 0
        self._partial = ""

    def poll(self) -> LaunchState:
        try:
            st = self.log_path.stat()
        except FileNotFoundError:
            return self.state
        if st.st_mtime + 1 < self.started_at:
            return self.state  # still the log from the previous launch
        if st.st_size < self._offset:  # truncated by a fresh start
            self._offset, self._partial = 0, ""
            self.state = LaunchState()
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except OSError:
            return self.state
        if self.state.phase == "waiting":
            self.state.phase = "starting"
        text = self._partial + chunk
        lines = text.split("\n")
        self._partial = lines.pop()  # last piece may be an unfinished line
        for line in lines:
            self._feed(line.rstrip("\r"))
        return self.state

    def _feed(self, line: str) -> None:
        if _READY in line:
            self.state.phase = "ready"
            return
        m = _LOADING.search(line)
        if m:
            self.state.loaded += 1
            self.state.last_mod = m.group("name")
            if self.state.phase != "ready":
                self.state.phase = "loading"
        elif line.startswith(("[Error", "[Fatal")) and len(self.state.errors) < 20:
            self.state.errors.append(line)
