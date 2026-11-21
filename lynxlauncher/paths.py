"""Path safety, glob matching and per-user folders."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable

CASE_INSENSITIVE_FS = os.name == "nt"

_DRIVE = re.compile(r"^[A-Za-z]:")
_BAD_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}
_WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)
}


class UnsafePathError(ValueError):
    pass


def validate_rel_path(rel: str) -> str:
    """Make sure a manifest path is a plain relative path that is valid on Windows and Linux.

    Manifest paths always use forward slashes. Anything that could escape the
    profile folder (absolute paths, '..', drive letters) is rejected.
    """
    if not isinstance(rel, str) or not rel:
        raise UnsafePathError("empty path")
    if "\\" in rel:
        raise UnsafePathError("backslash in path")
    if rel.startswith("/") or _DRIVE.match(rel):
        raise UnsafePathError("absolute path")
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise UnsafePathError(f"bad path segment {part!r}")
        if any(c in _BAD_CHARS for c in part):
            raise UnsafePathError(f"character not allowed on Windows in {part!r}")
        if part != part.rstrip(" ."):
            raise UnsafePathError(f"trailing dot/space in {part!r}")
        if part.split(".")[0].lower() in _WIN_RESERVED:
            raise UnsafePathError(f"reserved Windows name {part!r}")
    return rel


def to_local(root: Path, rel: str) -> Path:
    validate_rel_path(rel)
    return Path(root).joinpath(*rel.split("/"))


def manifest_key(rel: str) -> str:
    """Key used to detect duplicates in a manifest: always case-insensitive, for Windows clients."""
    return rel.casefold()


def local_key(rel: str, case_insensitive: bool = CASE_INSENSITIVE_FS) -> str:
    return rel.casefold() if case_insensitive else rel


def compile_glob(pattern: str) -> re.Pattern[str]:
    """gitignore-flavoured glob -> regex.

    *  matches within one path segment, ** matches across segments, ? one character.
    A pattern with no slash matches a file name anywhere ("*.log").
    A pattern with a slash is anchored to the profile root ("BepInEx/config/*.cfg");
    a leading slash anchors a bare name ("/mods.yml"). A trailing slash means
    "everything under this folder". Matching ignores case.
    """
    pat = pattern.strip().replace("\\", "/")
    anchored = "/" in pat.rstrip("/")
    if pat.endswith("/"):
        pat += "**"
    pat = pat.lstrip("/")
    out: list[str] = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    body = "".join(out)
    if not anchored:
        body = "(?:.*/)?" + body
    return re.compile("^" + body + "$", re.IGNORECASE)


class PathMatcher:
    def __init__(self, patterns: Iterable[str] = ()):
        self.patterns = [p for p in patterns if p and p.strip()]
        self._res = [compile_glob(p) for p in self.patterns]

    def matches(self, rel: str) -> bool:
        return any(r.match(rel) for r in self._res)

    def matching_patterns(self, rel: str) -> list[str]:
        return [p for p, r in zip(self.patterns, self._res) if r.match(rel)]

    def __bool__(self) -> bool:
        return bool(self._res)


def user_data_dir() -> Path:
    override = os.environ.get("LYNX_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "LynxModLauncher"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "lynx-mod-launcher"


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
