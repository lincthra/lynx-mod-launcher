"""Self-update for the frozen launcher exe.

Each GitHub release of the launcher carries latest.json:
  {"version": "0.2.0",
   "assets": {"windows": {"url": "...LynxModLauncher.exe", "sha256": "...", "size": 123},
              "linux":   {"url": "...LynxModLauncher-linux", "sha256": "...", "size": 456}}}
fetched from https://github.com/OWNER/REPO/releases/latest/download/latest.json.

Swap strategy: download next to the exe as <exe>.new, verify size + SHA-256, then
  Windows: rename the running exe to <exe>.old (allowed while running), move .new into place;
           the .old file is deleted on the next start.
  Linux:   replace the file in place (the running process keeps its open copy).
Then start the new exe and exit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from .doorstop import child_env

log = logging.getLogger(__name__)


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    version: str
    url: str
    sha256: str
    size: int


def platform_key(platform: str | None = None) -> str:
    return "windows" if (platform or sys.platform).startswith("win") else "linux"


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_exe() -> Path:
    return Path(sys.executable).resolve()


def check(update_url: str, current: str, fetcher, platform: str | None = None) -> UpdateInfo | None:
    """Return the newer release for this platform, or None if up to date."""
    try:
        data = json.loads(fetcher.get_bytes(update_url))
        version = str(data["version"])
        asset = data["assets"][platform_key(platform)]
        info = UpdateInfo(version, urljoin(update_url, str(asset["url"])), str(asset["sha256"]).lower(), int(asset["size"]))
    except (KeyError, TypeError, ValueError) as e:
        raise UpdateError(f"latest.json is malformed: {e}") from e
    if not re.fullmatch(r"[0-9a-f]{64}", info.sha256):
        raise UpdateError("latest.json has a bad sha256")
    if parse_version(info.version) <= parse_version(current):
        return None
    return info


def cleanup_old(exe: Path | None = None) -> None:
    exe = exe or current_exe()
    for leftover in (exe.with_name(exe.name + ".old"), exe.with_name(exe.name + ".new")):
        try:
            leftover.unlink()
            log.info("removed %s", leftover)
        except FileNotFoundError:
            pass
        except OSError as e:  # previous process may still be exiting; try next time
            log.debug("couldn't remove %s: %s", leftover, e)


def apply(info: UpdateInfo, fetcher, exe: Path, progress: Callable[[int, int], None] | None = None,
          platform: str | None = None) -> Path:
    """Download, verify and swap in the new exe. Returns the exe path to start."""
    new = exe.with_name(exe.name + ".new")
    h = hashlib.sha256()
    done = 0
    try:
        with open(new, "wb") as f:
            def sink(chunk: bytes) -> None:
                nonlocal done
                done += len(chunk)
                if done > info.size:
                    raise UpdateError("update download is larger than expected")
                f.write(chunk)
                h.update(chunk)
                if progress:
                    progress(done, info.size)
            fetcher.download(info.url, sink)
        if done != info.size or h.hexdigest() != info.sha256:
            raise UpdateError("update download failed verification (size/hash mismatch)")
        if platform_key(platform) == "windows":
            old = exe.with_name(exe.name + ".old")
            try:
                old.unlink()
            except FileNotFoundError:
                pass
            os.replace(exe, old)
            try:
                os.replace(new, exe)
            except OSError:
                os.replace(old, exe)  # put the working exe back
                raise
        else:
            os.chmod(new, 0o755)
            os.replace(new, exe)
    except BaseException:
        try:
            new.unlink()
        except OSError:
            pass
        raise
    log.info("updated launcher to %s at %s", info.version, exe)
    return exe


def relaunch(exe: Path, argv: list[str]) -> None:
    """Start the (new) exe as a fresh process. The caller should exit right after."""
    env = child_env()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # don't reuse this process's unpacked bundle
    kwargs: dict = {"env": env, "close_fds": True, "cwd": str(exe.parent)}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    log.info("relaunching %s", exe)
    subprocess.Popen([str(exe), *argv], **kwargs)
