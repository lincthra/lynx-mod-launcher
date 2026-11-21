"""Follow BepInEx's LogOutput.log after Launch, so the splash screen can show real progress.

BepInEx writes <profile>/BepInEx/LogOutput.log (it truncates it on each start). We only trust
the file once its mtime is after the moment we clicked Launch, then read it incrementally.
Lines look like  "[Info   :   BepInEx] Loading [Some Mod 1.2.3]"  ("[Level : Source] text").

Two progress signals, because a profile's BepInEx.cfg may leave Info lines out of the disk log
([Logging.Disk] LogLevels), and the per-mod "Loading [...]" lines are Info:
  - "Loading [Mod x.y]" lines (Info)            -> exact count + current mod name
  - any line from a mod's own log source        -> "something is loading" + the mod's name
"Chainloader startup complete" (Message) marks the end either way.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_LINE = re.compile(r"^\[(?P<level>\w+)\s*:\s*(?P<source>[^\]]*?)\s*\]\s?(?P<text>.*)$")
_LOADING = re.compile(r"^Loading \[(?P<name>.+?)\]\s*$")
_READY = "Chainloader startup complete"
# Log sources that belong to BepInEx/Unity itself, not to a mod.
_CORE_SOURCES = {"bepinex", "preloader", "unity log", "harmonyx", "harmony", "mono", "doorstop", "monomod", ""}


@dataclass
class LaunchState:
    phase: str = "waiting"  # waiting -> starting -> loading -> ready
    loaded: int = 0         # "Loading [...]" lines seen (0 if Info isn't logged to disk)
    last_mod: str = ""      # newest mod name from either signal
    mod_sources: int = 0    # distinct mod log sources seen (fallback signal)
    errors: list[str] = field(default_factory=list)


class LaunchWatcher:
    """Re-reads the whole log on every poll and rebuilds the state from scratch.

    Reading incrementally from a saved offset broke in practice: BepInEx's preloader writes
    LogOutput.log, then the chainloader reopens it (AppendLog = false truncates it) and replays the
    preloader lines. By the next poll the new file is already longer than our old offset, so a
    "did it shrink?" check never fires and we'd skip straight past every "Loading [...]" line.
    The log is a few hundred KB at most while loading, so a full re-read every half second is cheap.
    """

    MAX_BYTES = 16 * 1024 * 1024  # don't slurp a runaway log; just keep the last state

    def __init__(self, log_path: Path, started_at: float | None = None):
        self.log_path = Path(log_path)
        self.started_at = time.time() if started_at is None else started_at
        self.state = LaunchState()
        self._sources: set[str] = set()

    def poll(self) -> LaunchState:
        try:
            st = self.log_path.stat()
        except FileNotFoundError:
            return self.state
        if st.st_mtime + 1 < self.started_at:
            return self.state  # still the log from the previous launch
        if st.st_size > self.MAX_BYTES:
            return self.state
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return self.state
        self.state, self._sources = LaunchState(phase="starting"), set()
        lines = text.split("\n")
        lines.pop()  # last piece may be an unfinished line (empty if the file ends in \n)
        for line in lines:
            self._feed(line.rstrip("\r"))
        return self.state
    def _feed(self, line: str) -> None:
        st = self.state
        if _READY in line:
            st.phase = "ready"
            return
        m = _LINE.match(line)
        if not m:
            return
        level, source, text = m.group("level").lower(), m.group("source"), m.group("text")
        if level in ("error", "fatal") and len(st.errors) < 20:
            st.errors.append(line)
        lm = _LOADING.match(text)
        if lm:
            st.loaded += 1
            st.last_mod = lm.group("name")
        elif source.lower() not in _CORE_SOURCES:
            if source not in self._sources:
                self._sources.add(source)
                st.mod_sources = len(self._sources)
            if not st.loaded:  # only use the fallback name when there are no Loading lines
                st.last_mod = source
        else:
            return
        if st.phase != "ready":
            st.phase = "loading"
