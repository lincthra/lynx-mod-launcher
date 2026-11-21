"""Tell Michael when mods in his Gale profile have newer versions on Thunderstore or Hexium.

Only informational: nothing here installs anything. He updates in Gale and publishes as usual.

Where the installed versions come from, best first:
  1. Gale's database (data.sqlite3, table `profiles`, column `mods`): each entry has
     "fullName" = "Owner-Name-1.2.3" and "backend" = "Thunderstore" | "Hexium", or for a mod
     imported from a zip, "author"/"name"/"version". Disabled mods are skipped. (Updates you
     told Gale to ignore still show here: Gale stores those by an ID the API doesn't return.)
  2. Fallback: BepInEx/plugins/<Owner-Name>/manifest.json ("version_number"); backend unknown.

Both sites speak the Thunderstore "experimental" API:
  https://thunderstore.io/api/experimental/package/<owner>/<name>/  -> {"latest": {"version_number": ...}}
  https://hexium.gg/api/experimental/package/<owner>/<name>/
A mod is checked on its own backend first; mods with no known backend (zip imports, the folder
fallback) are checked on both and the newer answer wins, since Michael publishes his own mods
to both but updates Hexium more often.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

API = {
    "thunderstore": "https://thunderstore.io/api/experimental/package/{owner}/{name}/",
    "hexium": "https://hexium.gg/api/experimental/package/{owner}/{name}/",
}
PAGE = {
    "thunderstore": "https://thunderstore.io/c/valheim/p/{owner}/{name}/",
    "hexium": "https://valheim.hexium.gg/mods/{owner}/{name}",
}
BACKENDS = ("thunderstore", "hexium")
# BepInExPack is managed by Gale itself and pinned per game; nagging about it isn't useful.
SKIP = {"denikson-BepInExPack_Valheim"}

_FULLNAME = re.compile(r"^(?P<owner>[^-]+)-(?P<name>.+)-(?P<version>\d+(?:\.\d+)*)$")


@dataclass(frozen=True)
class InstalledMod:
    owner: str
    name: str
    version: str
    backend: str | None = None  # "thunderstore", "hexium" or None (unknown: check both)

    @property
    def ident(self) -> str:
        return f"{self.owner}-{self.name}"


@dataclass(frozen=True)
class ModUpdate:
    mod: InstalledMod
    latest: str
    backend: str

    @property
    def url(self) -> str:
        return PAGE[self.backend].format(owner=self.mod.owner, name=self.mod.name)

    def line(self) -> str:
        where = "Hexium" if self.backend == "hexium" else "Thunderstore"
        return f"{self.mod.name} {self.mod.version} → {self.latest} ({where})"


def parse_version(v: str) -> tuple[int, ...]:
    parts = []
    for piece in str(v).strip().split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: str, installed: str) -> bool:
    return parse_version(latest) > parse_version(installed)


# -- reading what's installed ---------------------------------------------------


def gale_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "com.kesomannen.gale"


def _backend(value) -> str | None:
    v = str(value or "").strip().lower()
    return v if v in BACKENDS else None


def mods_from_gale_json(mods: list) -> list[InstalledMod]:
    out: list[InstalledMod] = []
    for m in mods if isinstance(mods, list) else []:
        if not isinstance(m, dict) or m.get("enabled") is False:
            continue
        full = m.get("fullName")
        if isinstance(full, str):
            fm = _FULLNAME.match(full)
            if fm:
                # entries without a "backend" key predate Hexium support: Thunderstore
                out.append(InstalledMod(fm["owner"], fm["name"], fm["version"], _backend(m.get("backend")) or "thunderstore"))
            continue
        # imported from a zip ("local"): author/name/version, backend unknown
        author, name, version = m.get("author"), m.get("name"), m.get("version")
        if isinstance(name, str) and name and version:
            out.append(InstalledMod(author if isinstance(author, str) else "", name, str(version), None))
    return out


def _query_profiles(db: Path) -> list[tuple[str, str, str]]:
    def run(path: Path):
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            return con.execute("SELECT name, path, mods FROM profiles").fetchall()
        finally:
            con.close()

    try:
        return run(db)
    except sqlite3.Error as e:
        # Gale holds the database open in WAL mode; if a read-only open fails, read a copy.
        log.debug("reading Gale's database directly failed (%s); trying a copy", e)
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in ("", "-wal", "-shm"):
                src = db.with_name(db.name + suffix)
                if src.exists():
                    shutil.copy2(src, Path(tmp) / (db.name + suffix))
            return run(Path(tmp) / db.name)


def read_gale_profile(profile_dir: Path, db: Path | None = None) -> list[InstalledMod] | None:
    """Mods Gale has in the profile at profile_dir, or None if Gale's database can't tell us."""
    db = db or gale_data_dir() / "data.sqlite3"
    if not db.is_file():
        return None
    try:
        rows = _query_profiles(db)
    except (sqlite3.Error, OSError) as e:
        log.warning("couldn't read Gale's database %s: %s", db, e)
        return None
    want = _norm(profile_dir)
    for name, path, mods in rows:
        if _norm(Path(path)) == want or (not path and name == profile_dir.name):
            try:
                return mods_from_gale_json(json.loads(mods))
            except (TypeError, ValueError):
                return None
    return None


def _norm(p: Path) -> str:
    try:
        return os.path.normcase(str(Path(p).expanduser().resolve()))
    except OSError:
        return os.path.normcase(str(p))


def read_plugin_folders(profile_dir: Path) -> list[InstalledMod]:
    out = []
    plugins = Path(profile_dir) / "BepInEx" / "plugins"
    for mf in sorted(plugins.glob("*/manifest.json")):
        owner, sep, name = mf.parent.name.partition("-")
        if not sep or not owner or not name:
            continue
        try:
            version = json.loads(mf.read_text("utf-8-sig")).get("version_number")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(version, str) and version:
            out.append(InstalledMod(owner, name, version, None))
    return out


def installed_mods(profile_dir: Path, db: Path | None = None) -> list[InstalledMod]:
    mods = read_gale_profile(Path(profile_dir), db)
    if mods is None:
        mods = read_plugin_folders(Path(profile_dir))
    else:
        # Zip imports often have no author in Gale; the plugin folder is named Owner-Name.
        owners = {f.name: f.owner for f in read_plugin_folders(Path(profile_dir))}
        mods = [InstalledMod(owners.get(m.name, ""), m.name, m.version, m.backend) if not m.owner else m for m in mods]
    return [m for m in mods if m.owner and m.ident not in SKIP]


# -- asking the sites ------------------------------------------------------------


def latest_version(get_bytes: Callable[[str], bytes], backend: str, owner: str, name: str) -> str | None:
    try:
        data = json.loads(get_bytes(API[backend].format(owner=owner, name=name)))
    except Exception as e:  # noqa: BLE001 - 404 (not on that site), offline, bad JSON: all "unknown"
        log.debug("%s %s-%s: %s", backend, owner, name, e)
        return None
    latest = data.get("latest") if isinstance(data, dict) else None
    v = latest.get("version_number") if isinstance(latest, dict) else None
    return v if isinstance(v, str) and v else None


def check(mods: list[InstalledMod], get_bytes: Callable[[str], bytes], workers: int = 8) -> list[ModUpdate]:
    def one(mod: InstalledMod) -> ModUpdate | None:
        best: tuple[str, str] | None = None
        for backend in ([mod.backend] if mod.backend else BACKENDS):
            v = latest_version(get_bytes, backend, mod.owner, mod.name)
            if v and is_newer(v, mod.version) and (best is None or is_newer(v, best[0])):
                best = (v, backend)
        return ModUpdate(mod, *best) if best else None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        found = [u for u in pool.map(one, mods) if u]
    return sorted(found, key=lambda u: u.mod.name.lower())


def summary(updates: list[ModUpdate], limit: int = 3) -> str:
    if not updates:
        return ""
    names = ", ".join(u.mod.name for u in updates[:limit])
    more = f" and {len(updates) - limit} more" if len(updates) > limit else ""
    noun = "update" if len(updates) == 1 else "updates"
    return f"{len(updates)} mod {noun} available in Gale: {names}{more}"
