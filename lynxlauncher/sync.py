"""The sync engine: make a profile folder match a manifest. No Valheim or GUI needed.

Order of operations, chosen so a failed update never leaves a half-updated profile:
  1. fetch + validate manifest.json
  2. plan: compare the profile against it (sizes first, SHA-256 only when sizes match,
     cached by size+mtime so a normal launch doesn't rehash everything)
  3. download every changed file into .lynx/staging and verify size + hash
  4. only if every download verified: delete extras, move staged files into place

What counts as an "extra" (deleted):
  - anything in a mirror folder (BepInEx/plugins, patchers, core) that isn't in the manifest
  - elsewhere, only files a previous sync installed that have since left the manifest.
    Files mods create themselves in BepInEx/config (per-character stats, caches) stay.
Files matching the manifest's keep patterns are installed once and then never touched.

Outside the mirror folders, a file that differs from the manifest is only replaced when the
published copy itself changed since we last installed it (installed.json remembers the hash we
put there). Many mods re-save their .cfg every time the game starts, and re-downloading those on
every launch was pointless churn. Mirror folders (the mod binaries) are always kept exact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.error import HTTPError

from .manifest import FileEntry, Manifest, ManifestError, iter_files, sha256_file
from .paths import CASE_INSENSITIVE_FS, PathMatcher, fmt_size, local_key, to_local

log = logging.getLogger(__name__)

STATE_DIRNAME = ".lynx"
TMP_SUFFIX = ".lynx-tmp"

# Local-only files the launcher never deletes, even though they aren't in the manifest.
PROTECTED = [
    "BepInEx/LogOutput.log*",
    "BepInEx/ErrorLog*",
    "BepInEx/cache/",
    "*" + TMP_SUFFIX,
]


class ProfileLock:
    """OS-level lock on <profile>/.lynx/lock so two launcher windows never sync at once.
    Released automatically if the process dies."""

    def __init__(self, state_dir: Path, timeout: float = 120.0):
        self.path = state_dir / "lock"
        self.timeout = timeout
        self._f = None

    def _try(self) -> bool:
        assert self._f is not None
        try:
            if os.name == "nt":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __enter__(self) -> "ProfileLock":
        import time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while not self._try():
            if time.monotonic() >= deadline:
                self._f.close()
                raise SyncError("Another Lynx Mod Launcher window is already updating. Close it and try again.")
            time.sleep(0.5)
        return self

    def __exit__(self, *exc) -> None:
        if self._f:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._f.seek(0)
                    msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._f.close()
            self._f = None


class Fetcher(Protocol):
    def get_bytes(self, url: str) -> bytes: ...
    def download(self, url: str, sink: Callable[[bytes], None]) -> None: ...


class SyncError(RuntimeError):
    pass


@dataclass
class Progress:
    phase: str  # "manifest" | "scan" | "download" | "apply"
    done: int = 0
    total: int = 0
    message: str = ""


ProgressFn = Callable[[Progress], None]


@dataclass
class SyncPlan:
    download: list[FileEntry] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    local_changes: list[str] = field(default_factory=list)  # left alone, counted in up_to_date
    up_to_date: int = 0

    @property
    def download_bytes(self) -> int:
        return sum(e.size for e in {e.sha256: e for e in self.download}.values())  # empty files add 0

    @property
    def is_noop(self) -> bool:
        return not self.download and not self.delete


@dataclass
class SyncResult:
    updated: int = 0
    deleted: int = 0
    kept: int = 0
    up_to_date: int = 0
    offline: bool = False
    error: str | None = None
    manifest_version: str | None = None
    manifest_name: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def headline(self) -> str:
        if self.offline:
            return "Couldn't reach the mod server"
        if self.error:
            return "Update failed"
        if self.updated or self.deleted:
            s = f"Updated {self.updated} file{'s' if self.updated != 1 else ''}"
            if self.deleted:
                s += f", removed {self.deleted}"
            return s
        return "Up to date"

    def detail(self) -> str:
        if self.error:
            return self.error
        if not self.manifest_version:
            return ""
        name = f"{self.manifest_name} · " if self.manifest_name else ""
        return f"{name}version {self.manifest_version}"


class HashCache:
    """rel path -> [size, mtime_ns, sha256], so unchanged files aren't rehashed every launch."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list] = {}
        self.dirty = False
        try:
            raw = json.loads(path.read_text("utf-8"))
            if isinstance(raw, dict):
                self.data = raw
        except (OSError, ValueError):
            pass

    def sha(self, rel: str, full: Path) -> str:
        st = full.stat()
        rec = self.data.get(rel)
        if isinstance(rec, list) and len(rec) == 3 and rec[0] == st.st_size and rec[1] == st.st_mtime_ns:
            return rec[2]
        sha, size = sha256_file(full)
        self.data[rel] = [size, st.st_mtime_ns, sha]
        self.dirty = True
        return sha

    def matches(self, rel: str, full: Path, entry: FileEntry) -> bool:
        if full.stat().st_size != entry.size:
            return False
        return self.sha(rel, full) == entry.sha256

    def record(self, rel: str, full: Path, sha: str) -> None:
        st = full.stat()
        self.data[rel] = [st.st_size, st.st_mtime_ns, sha]
        self.dirty = True

    def forget(self, rel: str) -> None:
        if self.data.pop(rel, None) is not None:
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + TMP_SUFFIX)
        tmp.write_text(json.dumps(self.data), "utf-8")
        os.replace(tmp, self.path)
        self.dirty = False


def _noop(_: Progress) -> None:
    pass


class Syncer:
    def __init__(
        self,
        profile_dir: Path,
        fetcher: Fetcher,
        workers: int = 4,
        case_insensitive: bool = CASE_INSENSITIVE_FS,
    ):
        self.profile = Path(profile_dir)
        self.state = self.profile / STATE_DIRNAME
        self.fetcher = fetcher
        self.workers = max(1, workers)
        self.case_insensitive = case_insensitive
        self.cache = HashCache(self.state / "hashcache.json")
        self.installed_path = self.state / "installed.json"

    def _load_installed(self) -> dict[str, str]:
        """{key: sha256 we installed}. The old format was a plain list of paths (hash unknown: "")."""
        try:
            data = json.loads(self.installed_path.read_text("utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if isinstance(data, dict):
            return {self._key(p): h for p, h in data.items() if isinstance(p, str) and isinstance(h, str)}
        if isinstance(data, list):
            return {self._key(p): "" for p in data if isinstance(p, str)}
        return {}

    def _save_installed(self, manifest: Manifest) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.installed_path.with_name(self.installed_path.name + TMP_SUFFIX)
        tmp.write_text(json.dumps({e.path: e.sha256 for e in sorted(manifest.files, key=lambda e: e.path)},
                                  indent=0), "utf-8")
        os.replace(tmp, self.installed_path)

    # -- steps -------------------------------------------------------------

    def fetch_manifest(self, url: str) -> Manifest:
        return Manifest.from_json(self.fetcher.get_bytes(url))

    def _key(self, rel: str) -> str:
        return local_key(rel, self.case_insensitive)

    def plan(self, manifest: Manifest, progress: ProgressFn = _noop) -> SyncPlan:
        keep = PathMatcher(manifest.keep)
        protect = PathMatcher(PROTECTED)
        mirror = PathMatcher(manifest.mirror)
        installed = self._load_installed()
        local = {self._key(rel): (rel, full) for rel, full in iter_files(self.profile, [STATE_DIRNAME])}
        plan = SyncPlan()
        wanted: set[str] = set()
        for i, entry in enumerate(manifest.files):
            progress(Progress("scan", i, len(manifest.files), "Checking your files"))
            key = self._key(entry.path)
            wanted.add(key)
            hit = local.get(key)
            if hit is None:
                plan.download.append(entry)
                continue
            rel, full = hit
            if keep.matches(entry.path):
                plan.kept.append(rel)  # personal file: installed once, then left alone
            elif self.cache.matches(rel, full, entry):
                plan.up_to_date += 1
            elif not mirror.matches(entry.path) and installed.get(key) == entry.sha256:
                # Changed locally (usually a mod re-saving its config at startup), but the
                # published copy is the same one we installed last time: leave it.
                plan.local_changes.append(rel)
                plan.up_to_date += 1
            else:
                plan.download.append(entry)
        for key, (rel, _full) in local.items():
            if key in wanted:
                continue
            if keep.matches(rel):
                plan.kept.append(rel)
            elif protect.matches(rel):
                continue
            elif mirror.matches(rel) or key in installed:
                plan.delete.append(rel)
            # else: a file the game or a mod created (e.g. per-character data in config/) — leave it
        self.cache.save()
        return plan

    def apply(self, manifest: Manifest, plan: SyncPlan, manifest_url: str, progress: ProgressFn = _noop) -> None:
        staging = self.state / "staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            self._download_all(manifest, plan, manifest_url, staging, progress)
            self._install(plan, staging, progress)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            self.cache.save()

    def _download_all(self, manifest, plan, manifest_url, staging, progress) -> None:
        # Empty files aren't stored (GitHub rejects zero-byte assets); they're created locally.
        unique = list({e.sha256: e for e in plan.download if e.size > 0}.values())
        total = sum(e.size for e in unique)
        done = 0
        lock = threading.Lock()
        cancel = threading.Event()

        def fetch_one(entry: FileEntry) -> None:
            nonlocal done
            url = manifest.object_url(manifest_url, entry.sha256)
            h = hashlib.sha256()
            size = 0
            with open(staging / entry.sha256, "wb") as f:

                def sink(chunk: bytes) -> None:
                    nonlocal size, done
                    if cancel.is_set():
                        raise SyncError("cancelled")
                    size += len(chunk)
                    if size > entry.size:
                        raise SyncError(f"Download of {entry.path} was corrupted (too much data).")
                    f.write(chunk)
                    h.update(chunk)
                    with lock:
                        done += len(chunk)
                        d = done
                    progress(Progress("download", d, total, entry.path))

                try:
                    self.fetcher.download(url, sink)
                except HTTPError as e:
                    raise SyncError(f"Couldn't download {entry.path} (HTTP {e.code}).") from e
            if size != entry.size or h.hexdigest() != entry.sha256:
                raise SyncError(f"Download of {entry.path} was corrupted (hash mismatch).")

        progress(Progress("download", 0, total, f"Downloading {len(unique)} files ({fmt_size(total)})"))
        with ThreadPoolExecutor(self.workers) as pool:
            futures = [pool.submit(fetch_one, e) for e in unique]
            first_error: BaseException | None = None
            for fut in as_completed(futures):
                try:
                    fut.result()
                except BaseException as e:  # noqa: BLE001 - re-raised below
                    cancel.set()
                    if first_error is None or str(first_error) == "cancelled":
                        first_error = e
            if first_error is not None:
                raise first_error

    def _install(self, plan: SyncPlan, staging: Path, progress: ProgressFn) -> None:
        steps = len(plan.delete) + len(plan.download)
        step = 0
        progress(Progress("apply", 0, steps, "Installing"))
        for rel in plan.delete:
            path = self.profile.joinpath(*rel.split("/"))
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except PermissionError as e:
                raise SyncError(f"Couldn't remove {rel}. Is Valheim still running?") from e
            self.cache.forget(rel)
            step += 1
            progress(Progress("apply", step, steps, rel))
        self._prune_empty_dirs()
        remaining: dict[str, int] = {}
        for entry in plan.download:
            remaining[entry.sha256] = remaining.get(entry.sha256, 0) + 1
        for entry in plan.download:
            dest = to_local(self.profile, entry.path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + TMP_SUFFIX)
            remaining[entry.sha256] -= 1
            staged = staging / entry.sha256
            if entry.size == 0:
                tmp.write_bytes(b"")
            elif remaining[entry.sha256] == 0:
                shutil.move(staged, tmp)  # last user of this download: move, don't copy
            else:
                shutil.copyfile(staged, tmp)
            try:
                os.replace(tmp, dest)
            except PermissionError as e:
                tmp.unlink(missing_ok=True)
                raise SyncError(f"Couldn't replace {entry.path}. Is Valheim still running?") from e
            self.cache.record(entry.path, dest, entry.sha256)
            step += 1
            progress(Progress("apply", step, steps, entry.path))
        self._prune_empty_dirs()

    def _prune_empty_dirs(self) -> None:
        for dirpath, _dirs, _files in os.walk(self.profile, topdown=False):
            p = Path(dirpath)
            if p == self.profile or p == self.state or self.state in p.parents:
                continue
            try:
                p.rmdir()  # only succeeds when empty
            except OSError:
                pass

    # -- all together ------------------------------------------------------

    def run(self, manifest_url: str, progress: ProgressFn = _noop, lock_timeout: float = 120.0) -> SyncResult:
        try:
            with ProfileLock(self.state, lock_timeout):
                return self._run(manifest_url, progress)
        except SyncError as e:  # lock not acquired
            return SyncResult(error=str(e))

    def _run(self, manifest_url: str, progress: ProgressFn) -> SyncResult:
        progress(Progress("manifest", message="Checking for updates"))
        try:
            manifest = self.fetch_manifest(manifest_url)
        except ManifestError as e:
            log.error("bad manifest: %s", e)
            return SyncResult(error=str(e))
        except HTTPError as e:
            log.error("manifest fetch failed: HTTP %s", e.code)
            return SyncResult(offline=True, error=f"The mod server answered HTTP {e.code}. You can still launch with the mods you already have.")
        except (OSError, ValueError) as e:
            log.warning("manifest fetch failed: %s", e)
            return SyncResult(offline=True, error="You can still launch with the mods you already have. (Details are in the log.)")

        self.profile.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        plan = self.plan(manifest, progress)
        log.info(
            "manifest %s: %d files, %d up to date, %d to download (%s), %d to delete, %d kept",
            manifest.version, len(manifest.files), plan.up_to_date, len(plan.download),
            fmt_size(plan.download_bytes), len(plan.delete), len(plan.kept),
        )
        for e in plan.download:
            log.info("  download %s", e.path)
        for rel in plan.delete:
            log.info("  delete   %s", rel)
        for rel in plan.local_changes:
            log.debug("  changed locally, published copy unchanged: %s", rel)
        result = SyncResult(manifest_version=manifest.version, manifest_name=manifest.name or None,
                            up_to_date=plan.up_to_date, kept=len(plan.kept))
        if not plan.is_noop:
            try:
                self.apply(manifest, plan, manifest_url, progress)
            except (SyncError, OSError) as e:
                log.error("update failed: %s", e)
                result.error = f"{str(e).rstrip('.')}. You can still launch with your current files."
                return result
        result.updated = len(plan.download)
        result.deleted = len(plan.delete)
        self._save_installed(manifest)
        try:
            (self.state / "manifest.json").write_text(manifest.to_json(), "utf-8")
        except OSError:
            pass
        return result
