"""manifest.json: the list of files (path, size, SHA-256) that make up the published modpack.

The files themselves are stored content-addressed (named by SHA-256), either
  - next to manifest.json as objects/<first 2 hex>/<sha256>  (any static web host), or
  - at a URL template such as
    https://github.com/OWNER/REPO/releases/download/objects-{sha1}/{sha}  (GitHub Releases).
The publisher uploads files first and swaps manifest.json last, so a friend who is
mid-download never sees a half-published modpack.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urljoin

from .paths import PathMatcher, UnsafePathError, manifest_key, validate_rel_path

SCHEMA_VERSION = 1

# Folders the launcher owns outright: anything in them that isn't in the manifest is removed.
# Everywhere else (BepInEx/config, the profile root) the launcher only removes files it
# installed itself, so per-character data that mods write into config/ is never touched.
DEFAULT_MIRROR = ["BepInEx/plugins/", "BepInEx/patchers/", "BepInEx/core/"]
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    sha256: str


@dataclass
class Manifest:
    name: str
    version: str
    files: list[FileEntry]
    keep: list[str] = field(default_factory=list)
    mirror: list[str] = field(default_factory=lambda: list(DEFAULT_MIRROR))
    objects: str = "objects/"
    generated: str = ""
    schema: int = SCHEMA_VERSION

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def object_relpath(self, sha256: str) -> str:
        base = self.objects if self.objects.endswith("/") else self.objects + "/"
        return f"{base}{sha256[:2]}/{sha256}"

    def object_url(self, manifest_url: str, sha256: str) -> str:
        if "{sha}" in self.objects:
            return self.objects.replace("{sha}", sha256).replace("{sha1}", sha256[:1]).replace("{sha2}", sha256[:2])
        return urljoin(manifest_url, self.object_relpath(sha256))

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "name": self.name,
            "version": self.version,
            "generated": self.generated,
            "objects": self.objects,
            "keep": list(self.keep),
            "mirror": list(self.mirror),
            "files": [asdict(f) for f in self.files],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1) + "\n"

    @classmethod
    def from_json(cls, text: str | bytes) -> "Manifest":
        try:
            if isinstance(text, bytes):
                text = text.decode("utf-8")
            data = json.loads(text)
        except ValueError as e:
            raise ManifestError(f"manifest is not valid JSON: {e}") from e
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: object) -> "Manifest":
        if not isinstance(data, dict):
            raise ManifestError("manifest must be a JSON object")
        schema = data.get("schema")
        if not isinstance(schema, int) or isinstance(schema, bool):
            raise ManifestError("manifest has no schema number")
        if schema > SCHEMA_VERSION:
            raise ManifestError(
                "This modpack needs a newer Lynx Mod Launcher. Ask Michael for the latest version."
            )
        files_raw = data.get("files")
        if not isinstance(files_raw, list):
            raise ManifestError("manifest has no file list")
        entries: list[FileEntry] = []
        seen: dict[str, str] = {}
        for item in files_raw:
            if not isinstance(item, dict):
                raise ManifestError("bad file entry")
            path, size, sha = item.get("path"), item.get("size"), item.get("sha256")
            try:
                validate_rel_path(path)  # type: ignore[arg-type]
            except UnsafePathError as e:
                raise ManifestError(f"unsafe path in manifest: {path!r} ({e})") from e
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ManifestError(f"bad size for {path}")
            if not isinstance(sha, str) or not _HEX64.match(sha):
                raise ManifestError(f"bad sha256 for {path}")
            key = manifest_key(path)  # type: ignore[arg-type]
            if key in seen:
                raise ManifestError(f"{path!r} and {seen[key]!r} differ only by case")
            seen[key] = path  # type: ignore[assignment]
            entries.append(FileEntry(path, size, sha))  # type: ignore[arg-type]
        check_tree_conflicts(seen)
        keep = data.get("keep", [])
        if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep):
            raise ManifestError("keep must be a list of patterns")
        mirror = data.get("mirror", DEFAULT_MIRROR)
        if not isinstance(mirror, list) or not all(isinstance(k, str) for k in mirror):
            raise ManifestError("mirror must be a list of patterns")
        objects = data.get("objects", "objects/")
        if not isinstance(objects, str):
            raise ManifestError("objects must be a string")
        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            files=entries,
            keep=list(keep),
            mirror=list(mirror),
            objects=objects,
            generated=str(data.get("generated", "")),
            schema=schema,
        )


def check_tree_conflicts(keys: Iterable[str]) -> None:
    """Reject manifests where one path is both a file and a folder (a and a/b)."""
    keyset = set(keys)
    for key in keyset:
        parts = key.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in keyset:
                raise ManifestError(f"{'/'.join(parts[:i])!r} is both a file and a folder")


def sha256_file(path: Path, chunk: int = 1 << 20) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def iter_files(root: Path, skip_dirs: Iterable[str] = ()) -> Iterator[tuple[str, Path]]:
    """Yield (relative posix path, full path) for every regular file under root, sorted."""
    root = Path(root)
    skip = {s.casefold() for s in skip_dirs}
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(
            d for d in dirnames if (f"{rel_dir}/{d}" if rel_dir else d).casefold() not in skip
        )
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_file():
                yield (f"{rel_dir}/{name}" if rel_dir else name), full


def build_manifest(
    source: Path,
    name: str,
    exclude: PathMatcher,
    keep: list[str],
    generated: str = "",
    objects: str = "objects/",
) -> tuple[Manifest, dict[str, Path]]:
    """Hash a folder into a Manifest. Returns the manifest and path -> source file."""
    source = Path(source)
    if not source.is_dir():
        raise ManifestError(f"source folder not found: {source}")
    files: list[FileEntry] = []
    sources: dict[str, Path] = {}
    for rel, full in iter_files(source):
        if exclude.matches(rel):
            continue
        try:
            validate_rel_path(rel)
        except UnsafePathError as e:
            raise ManifestError(f"{rel!r} can't be published: {e}") from e
        sha, size = sha256_file(full)
        files.append(FileEntry(rel, size, sha))
        sources[rel] = full
    files.sort(key=lambda e: e.path.casefold())
    digest = hashlib.sha256()
    for e in files:
        digest.update(f"{e.path}\0{e.size}\0{e.sha256}\n".encode())
    for k in keep:
        digest.update(f"keep\0{k}\n".encode())
    manifest = Manifest(name, digest.hexdigest()[:12], files, list(keep), generated=generated, objects=objects)
    Manifest.from_dict(manifest.to_dict())  # same validation the client runs
    return manifest, sources
