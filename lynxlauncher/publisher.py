"""lynx-publish: hash a Gale profile, publish manifest + files, optionally push to the game server.

Destinations:
  github:OWNER/REPO       GitHub Releases on a public repo (needs the gh CLI, logged in)
  /some/folder            a folder your web server serves
  user@host:/some/folder  the same, over rsync/SSH
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .manifest import Manifest, ManifestError, build_manifest, iter_files, sha256_file
from .paths import PathMatcher, UnsafePathError, fmt_size, validate_rel_path

# Never published: logs, caches, backups, Gale/r2modman bookkeeping, launcher temp files.
DEFAULT_EXCLUDE = [
    "*.log",
    "BepInEx/LogOutput.log*",
    "BepInEx/ErrorLog*",
    "BepInEx/cache/",
    "BepInEx/LMF_StartupReport.txt",  # LynxModFixes writes it on every start
    "*.old",
    "*.bak",
    "/mods.yml",
    "/export.r2x",
    "*.lynx-tmp",
    "*.lynx-bak",
    ".lynx/",
]

# Publishing stops if a file looks like it holds secrets, unless it's excluded (the usual
# fix) or listed in allow_secrets. The GitHub destination is public.
SECRET_PATTERNS = ["*password*", "*passwd*", "*secret*", "*token*", "*credential*", "*.pem", "*.key"]
# Mod binaries and asset bundles come from public packages, so names like
# AutoServerPassword.dll or mwl_secretroom1 aren't secrets. Only check everything else
# (BepInEx/config, the profile root), which is where mods write passwords.
SECRET_SKIP = ["BepInEx/plugins/", "BepInEx/patchers/", "BepInEx/core/"]

GALE_PROFILES = Path("~/.local/share/com.kesomannen.gale/valheim/profiles")

Out = Callable[[str], None]


def detail(out: Out, msg: str) -> None:
    """Log-file-only output (full file lists, every rsync line). No-op for plain callables."""
    fn = getattr(out, "detail", None)
    if fn:
        fn(msg)


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "lynx-mod-launcher"


class PublishLog:
    """Console gets the summary; the log file gets everything. Keeps the newest `keep` logs."""

    def __init__(self, log_dir: Path | None = None, keep: int = 30):
        self.dir = log_dir or state_dir() / "publish-logs"
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.path = self.dir / f"publish-{stamp}.log"
        self._f = open(self.path, "w", encoding="utf-8")
        old = sorted(self.dir.glob("publish-*.log"))[:-keep]
        for f in old:
            f.unlink(missing_ok=True)

    def __call__(self, msg: str) -> None:
        print(msg, flush=True)
        self.detail(msg)

    def detail(self, msg: str) -> None:
        self._f.write(msg.rstrip("\n") + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


@dataclass
class ServerConfig:
    enabled: bool = False
    dest: str = ""
    paths: list[str] = field(default_factory=lambda: ["BepInEx/plugins"])
    exclude: list[str] = field(default_factory=list)
    delete: bool = False
    rsync_args: list[str] = field(default_factory=list)
    # Only update mod folders the server already has; never add new ones. Client-only
    # mods stay off the server without having to list them. Add a new server mod once by
    # hand (or with existing_only = false for one run) and it's kept in sync after that.
    existing_only: bool = True
    restart: "RestartConfig | None" = None  # [server.restart]: restart once nobody's on


@dataclass
class PublishConfig:
    source: Path
    dest: str
    name: str = "Modpack"
    keep: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    allow_secrets: list[str] = field(default_factory=list)
    rsync_args: list[str] = field(default_factory=list)
    server: ServerConfig = field(default_factory=ServerConfig)


class PublishError(RuntimeError):
    pass


def _str_list(d: dict, key: str) -> list[str]:
    v = d.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise PublishError(f"'{key}' must be a list of strings")
    return v


def gale_profile_path(name: str, gale_profiles: str | None = None) -> Path:
    base = Path(os.path.expandvars(gale_profiles or os.environ.get("LYNX_GALE_PROFILES") or str(GALE_PROFILES)))
    return base.expanduser() / name


def _restart_config(d, server_dest: str):
    from .restarter import RestartConfig

    if d is None:
        return None
    if not isinstance(d, dict):
        raise PublishError("[server.restart] must be a table")
    for key in ("panel", "server_id"):
        if not isinstance(d.get(key), str) or not d[key]:
            raise PublishError(f"[server.restart] '{key}' is required")
    query = str(d.get("query", ""))
    if not query:  # default: the host from [server] dest, port 2457
        host = server_dest.split(":", 1)[0].rsplit("@", 1)[-1] if is_remote(server_dest) else ""
        if not host:
            raise PublishError("[server.restart] set 'query' (e.g. \"10.0.0.99:2457\")")
        query = f"{host}:2457"
    host, _, port = query.rpartition(":")
    if not host or not port.isdigit():
        raise PublishError(f"[server.restart] query must look like host:port, not {query!r}")
    return RestartConfig(
        panel=d["panel"], server_id=d["server_id"], query_host=host, query_port=int(port),
        api_key_file=str(d.get("api_key_file", "")),
        poll_minutes=float(d.get("poll_minutes", 5)), empty_checks=int(d.get("empty_checks", 2)),
        give_up_hours=float(d.get("give_up_hours", 24)),
    )


def load_config(path: Path, profile: str | None = None) -> PublishConfig:
    """profile (e.g. from --profile) overrides the file's profile/source."""
    try:
        data = tomllib.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        raise PublishError(f"settings file not found: {path} (copy publish.example.toml)") from None
    except tomllib.TOMLDecodeError as e:
        raise PublishError(f"{path}: {e}") from e
    if not isinstance(data.get("dest"), str) or not data["dest"]:
        raise PublishError(f"{path}: 'dest' is required")
    profile = profile or data.get("profile")
    if profile:
        source = gale_profile_path(str(profile), data.get("gale_profiles"))
    elif isinstance(data.get("source"), str) and data["source"]:
        source = Path(os.path.expandvars(data["source"])).expanduser()
    else:
        raise PublishError(f"{path}: set 'profile' (a Gale profile name) or 'source' (a folder)")
    srv = data.get("server", {})
    if not isinstance(srv, dict):
        raise PublishError("[server] must be a table")
    server = ServerConfig(
        enabled=bool(srv.get("enabled", False)),
        dest=str(srv.get("dest", "")),
        paths=_str_list(srv, "paths") if "paths" in srv else ["BepInEx/plugins"],
        exclude=_str_list(srv, "exclude"),
        delete=bool(srv.get("delete", False)),
        existing_only=bool(srv.get("existing_only", True)),
        rsync_args=_str_list(srv, "rsync_args"),
        restart=_restart_config(srv.get("restart"), str(srv.get("dest", ""))),
    )
    return PublishConfig(
        source=source,
        dest=os.path.expandvars(data["dest"]),
        name=str(data.get("name") or profile or source.name),
        keep=_str_list(data, "keep"),
        exclude=_str_list(data, "exclude"),
        allow_secrets=_str_list(data, "allow_secrets"),
        rsync_args=_str_list(data, "rsync_args"),
        server=server,
    )


def github_repo(dest: str) -> str | None:
    m = re.match(r"^github:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$", dest.strip())
    return m.group(1) if m else None


def is_remote(dest: str) -> bool:
    """'host:/path' or 'user@host:path' -> rsync over ssh. 'C:\\x' and '/x' are local."""
    return github_repo(dest) is None and bool(re.match(r"^[^/\\:]{2,}:", dest))


_UNCHANGED = re.compile(r"^\.[fdL]\.\.[t.][p.][o.][g.]\.{3,} ")  # rsync -i: attribute-only lines


def _run(cmd: list[str], out: Out) -> int:
    """Run a command; every output line goes to the log, real changes also to the console.
    Returns the number of changed items rsync reported."""
    detail(out, "  $ " + " ".join(cmd))
    if shutil.which(cmd[0]) is None:
        raise PublishError(f"{cmd[0]} is not installed")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    changes = 0
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        tail = (tail + [line])[-15:]
        if line and not _UNCHANGED.match(line) and not line.startswith(("sending ", "sent ", "total size")):
            changes += 1
            out("    " + line)  # console + log
        else:
            detail(out, "    " + line)  # log only
    if proc.wait() != 0:
        raise PublishError(f"{cmd[0]} failed with exit code {proc.returncode}:\n" + "\n".join(tail))
    return changes


def _copy_verified(src: Path, dst: Path, sha: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".lynx-tmp")
    shutil.copyfile(src, tmp)
    got, _ = sha256_file(tmp)
    if got != sha:
        tmp.unlink(missing_ok=True)
        raise PublishError(f"{src} changed while publishing. Close Gale/the game and run again.")
    os.replace(tmp, dst)


def _write_objects(root: Path, manifest: Manifest, sources: dict[str, Path]) -> int:
    new = 0
    for e in manifest.files:
        if e.size == 0:
            continue  # empty files aren't stored; the launcher creates them
        obj = root / manifest.object_relpath(e.sha256)
        if obj.is_file() and obj.stat().st_size == e.size:
            continue
        _copy_verified(sources[e.path], obj, e.sha256)
        new += 1
    return new


def _publish_local(dest: Path, manifest: Manifest, sources: dict[str, Path], prune: bool, out: Out) -> None:
    new = _write_objects(dest, manifest, sources)
    tmp = dest / "manifest.json.lynx-tmp"
    tmp.write_text(manifest.to_json(), "utf-8")
    os.replace(tmp, dest / "manifest.json")  # swapped last, so clients never see missing objects
    out(f"  {new} new object(s) written to {dest}")
    if prune:
        referenced = {e.sha256 for e in manifest.files}
        removed = 0
        for obj in (dest / "objects").glob("*/*"):
            if obj.is_file() and obj.name not in referenced:
                obj.unlink()
                removed += 1
        for d in (dest / "objects").glob("*"):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        out(f"  pruned {removed} unreferenced object(s)")


def _publish_remote(dest: str, cfg: PublishConfig, manifest: Manifest, sources: dict[str, Path], prune: bool, out: Out) -> None:
    dest = dest.rstrip("/") + "/"
    with tempfile.TemporaryDirectory(prefix="lynx-") as td:
        stage = Path(td)
        _write_objects(stage, manifest, sources)
        (stage / "manifest.json").write_text(manifest.to_json(), "utf-8")
        base = ["rsync", "-rt", "--chmod=D755,F644", *cfg.rsync_args]
        objs = base + ["--ignore-existing"] + (["--delete"] if prune else []) + [str(stage / "objects"), dest]
        _run(objs, out)
        _run(base + [str(stage / "manifest.json"), dest + "manifest.json"], out)


# --- GitHub Releases -------------------------------------------------------------------
# Files are release assets named by SHA-256, spread over 16 releases (objects-0 … objects-f,
# by first hex digit) to stay well under GitHub's 1000-assets-per-release limit.
# manifest.json is an asset of the "manifest" release and is replaced last.

GH_TAGS = [f"objects-{c}" for c in "0123456789abcdef"]
GH_MANIFEST_TAG = "manifest"
GH_NOTES = "Storage for Lynx Mod Launcher, managed by lynx-publish. Don't edit by hand."


def github_manifest_url(repo: str) -> str:
    return f"https://github.com/{repo}/releases/download/{GH_MANIFEST_TAG}/manifest.json"


def github_objects_template(repo: str) -> str:
    return f"https://github.com/{repo}/releases/download/objects-{{sha1}}/{{sha}}"


class GitHub:
    def __init__(self, repo: str, out: Out, gh: str | None = None):
        self.repo = repo
        self.out = out
        self.gh = gh or os.environ.get("LYNX_GH") or "gh"
        if shutil.which(self.gh) is None:
            raise PublishError("the GitHub CLI (gh) is not installed. On CachyOS: sudo pacman -S github-cli, then gh auth login")

    def run(self, *args: str, capture: bool = False) -> str:
        proc = subprocess.run([self.gh, *args, "--repo", self.repo], capture_output=True, text=True)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip()
            raise PublishError(f"gh {' '.join(args[:2])} failed: {msg}")
        return proc.stdout if capture else ""

    def releases(self) -> set[str]:
        data = json.loads(self.run("release", "list", "--limit", "200", "--json", "tagName", capture=True) or "[]")
        return {r["tagName"] for r in data}

    def ensure_releases(self, tags: list[str]) -> None:
        have = self.releases()
        for tag in tags:
            if tag not in have:
                self.out(f"  creating release {tag}")
                try:
                    self.run("release", "create", tag, "--title", tag, "--notes", GH_NOTES, "--latest=false")
                except PublishError as e:
                    raise PublishError(f"{e}\n(The repo needs at least one commit, e.g. a README, before releases can be created.)") from None

    def assets(self, tag: str) -> set[str]:
        data = json.loads(self.run("release", "view", tag, "--json", "assets", capture=True) or "{}")
        return {a["name"] for a in data.get("assets", [])}

    def upload(self, tag: str, files: list[Path], clobber: bool = False) -> None:
        for i in range(0, len(files), 50):
            chunk = [str(f) for f in files[i:i + 50]]
            self.run("release", "upload", tag, *chunk, *(["--clobber"] if clobber else []))

    def delete_asset(self, tag: str, name: str) -> None:
        self.run("release", "delete-asset", tag, name, "--yes")


def _publish_github(repo: str, manifest: Manifest, sources: dict[str, Path], prune: bool, out: Out) -> None:
    gh = GitHub(repo, out)
    gh.ensure_releases(GH_TAGS + [GH_MANIFEST_TAG])
    by_tag: dict[str, dict[str, Path]] = {}
    for e in manifest.files:
        if e.size == 0:
            continue  # GitHub rejects zero-byte assets; the launcher creates empty files itself
        by_tag.setdefault(f"objects-{e.sha256[0]}", {})[e.sha256] = sources[e.path]
    referenced = {e.sha256 for e in manifest.files}
    new = 0
    with tempfile.TemporaryDirectory(prefix="lynx-gh-") as td:
        stage = Path(td)
        for tag in GH_TAGS:
            wanted = by_tag.get(tag, {})
            have = gh.assets(tag)
            missing = sorted(set(wanted) - have)
            if missing:
                files = []
                for sha in missing:
                    _copy_verified(wanted[sha], stage / sha, sha)
                    files.append(stage / sha)
                out(f"  {tag}: uploading {len(files)} file(s)")
                gh.upload(tag, files)
                new += len(files)
                for f in files:
                    f.unlink()
            if prune:
                for name in sorted(have - referenced):
                    gh.delete_asset(tag, name)
                if have - referenced:
                    out(f"  {tag}: pruned {len(have - referenced)} old file(s)")
        mpath = stage / "manifest.json"
        mpath.write_text(manifest.to_json(), "utf-8")
        gh.upload(GH_MANIFEST_TAG, [mpath], clobber=True)
    out(f"  {new} new file(s) uploaded")
    out(f"  Manifest URL for the launcher: {github_manifest_url(repo)}")


def pending_uploads(cfg: PublishConfig, manifest: Manifest, out: Out) -> list | None:
    """Files whose content isn't stored at the destination yet (None if we can't tell cheaply)."""
    files = [e for e in manifest.files if e.size > 0]
    repo = github_repo(cfg.dest)
    if repo:
        gh = GitHub(repo, out)
        try:
            tags = gh.releases()
        except PublishError:
            return None
        have: set[str] = set()
        for tag in GH_TAGS:
            if tag in tags:
                have |= gh.assets(tag)
        return [e for e in files if e.sha256 not in have]
    if is_remote(cfg.dest):
        return None
    root = Path(cfg.dest).expanduser()
    return [e for e in files if not (root / manifest.object_relpath(e.sha256)).is_file()]


def check_secrets(manifest: Manifest, allow: list[str]) -> list[str]:
    secret = PathMatcher(SECRET_PATTERNS)
    skip = PathMatcher(SECRET_SKIP)
    allowed = PathMatcher(allow)
    return [e.path for e in manifest.files
            if secret.matches(e.path) and not skip.matches(e.path) and not allowed.matches(e.path)]


def check_dest(dest: str) -> None:
    """Fail before any hashing if a local destination doesn't exist (catches typos)."""
    if github_repo(dest) or is_remote(dest):
        return
    if dest.strip().lower().startswith("github:"):
        raise PublishError(f"dest {dest!r}: GitHub destinations look like github:OWNER/REPO")
    path = Path(dest).expanduser()
    if not path.is_dir():
        raise PublishError(f"dest folder doesn't exist: {path}\nCreate it first (mkdir -p {path}) or fix 'dest' in your settings.")


def parse_rsync_list(output: str) -> set[str]:
    """Names from `rsync --list-only` output ("drwxr-xr-x  4,096 2026/10/17 18:59:00 Name")."""
    names = set()
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[4] not in (".", ".."):
            names.add(parts[4])
    return names


def server_entries(dest: str, sub: str, rsync_args: list[str]) -> set[str]:
    """Top-level names currently in <dest>/<sub> on the server (empty if the folder is missing)."""
    if is_remote(dest):
        args = [a for a in rsync_args if not a.startswith("--chown")]
        proc = subprocess.run(["rsync", "--list-only", *args, dest.rstrip("/") + f"/{sub}/"],
                              capture_output=True, text=True)
        if proc.returncode not in (0, 23):  # 23: folder doesn't exist yet
            raise PublishError(f"couldn't list {dest}/{sub}: {proc.stderr.strip()}")
        return parse_rsync_list(proc.stdout)
    root = Path(dest).expanduser().joinpath(*sub.split("/"))
    return {p.name for p in root.iterdir()} if root.is_dir() else set()


def push_to_server(cfg: PublishConfig, dry_run: bool, out: Out) -> int:
    """Returns how many files were (or would be) changed on the server."""
    srv = cfg.server
    total = 0
    if not srv.dest:
        raise PublishError("[server] dest is not set")
    exclude = PathMatcher(DEFAULT_EXCLUDE + cfg.exclude + srv.exclude)
    out(f"Server sync → {srv.dest}" + (" (dry run)" if dry_run else ""))
    for sub in srv.paths:
        try:
            validate_rel_path(sub)
        except UnsafePathError as e:
            raise PublishError(f"server path {sub!r}: {e}") from e
        src_root = cfg.source.joinpath(*sub.split("/"))
        files = {rel: full for rel, full in iter_files(src_root) if not exclude.matches(f"{sub}/{rel}")}
        protect: set[str] = set()
        if srv.existing_only:
            on_server = server_entries(srv.dest, sub, srv.rsync_args)
            ours = {rel.split("/", 1)[0] for rel in files}
            skipped = sorted(ours - on_server)
            if skipped:
                out(f"  {sub}: skipping {len(skipped)} not on the server (existing_only): {', '.join(skipped)}")
            files = {rel: full for rel, full in files.items() if rel.split("/", 1)[0] in on_server}
            protect = on_server - ours  # server-only mods: never touched, even with delete = true
        if is_remote(srv.dest):
            with tempfile.TemporaryDirectory(prefix="lynx-srv-") as td:
                stage = Path(td)
                for rel, full in files.items():
                    d = stage.joinpath(*sub.split("/"), *rel.split("/"))
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(full, d)
                stage.joinpath(*sub.split("/")).mkdir(parents=True, exist_ok=True)
                cmd = ["rsync", "-r", "--checksum", "--relative", "-i", *srv.rsync_args]
                if srv.delete:
                    cmd.append("--delete")
                    cmd += [f"--filter=P /{sub}/{name}" for name in sorted(protect)]
                if dry_run:
                    cmd.append("--dry-run")
                # "/./" tells --relative where the path to recreate on the server starts
                cmd += [f"{stage}/./{sub}/", srv.dest.rstrip("/") + "/"]
                n = _run(cmd, out)
                total += n
                out(f"  {sub}: {n} change(s)" + (" would be made" if dry_run else ""))
            continue
        dst_root = Path(srv.dest).expanduser().joinpath(*sub.split("/"))
        copied = removed = 0
        for rel, full in files.items():
            d = dst_root.joinpath(*rel.split("/"))
            sha, size = sha256_file(full)
            if d.is_file() and d.stat().st_size == size and sha256_file(d)[0] == sha:
                continue
            out(f"  {'would copy' if dry_run else 'copy'} {sub}/{rel}")
            if not dry_run:
                _copy_verified(full, d, sha)
            copied += 1
        if srv.delete:
            for rel, full in list(iter_files(dst_root)):
                if rel not in files and not exclude.matches(f"{sub}/{rel}") and rel.split("/", 1)[0] not in protect:
                    out(f"  {'would delete' if dry_run else 'delete'} {sub}/{rel}")
                    if not dry_run:
                        full.unlink()
                    removed += 1
        out(f"  {sub}: {copied} copied, {removed} deleted")
        total += copied + removed
    if total and not dry_run:
        out("  Restart the Valheim server so it picks up the new plugins.")
    return total


def publish(
    cfg: PublishConfig,
    *,
    dry_run: bool = False,
    list_files: bool = False,
    push_server: bool | None = None,
    prune: bool = False,
    out: Out = print,
) -> Manifest:
    exclude = PathMatcher(DEFAULT_EXCLUDE + cfg.exclude)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = github_repo(cfg.dest)
    objects = github_objects_template(repo) if repo else "objects/"
    if not cfg.source.is_dir():
        raise PublishError(f"profile folder not found: {cfg.source}")
    check_dest(cfg.dest)
    if push_server if push_server is not None else cfg.server.enabled:
        check_dest(cfg.server.dest)
    try:
        manifest, sources = build_manifest(cfg.source, cfg.name, exclude, cfg.keep, generated, objects)
    except ManifestError as e:
        raise PublishError(str(e)) from e
    secrets = check_secrets(manifest, cfg.allow_secrets)
    if secrets:
        raise PublishError(
            "these files look like they contain passwords or keys and would be published:\n    "
            + "\n    ".join(secrets)
            + "\nAdd them to 'exclude' (or to 'allow_secrets' if they're safe)."
        )
    out(f"{cfg.name}: {len(manifest.files)} files, {fmt_size(manifest.total_size)}, version {manifest.version}")
    keep = PathMatcher(cfg.keep)
    for pattern in cfg.keep:
        if not any(PathMatcher([pattern]).matches(e.path) for e in manifest.files):
            out(f"  note: keep pattern {pattern!r} matches nothing in the profile (fine for files the game creates)")
    for e in manifest.files:  # full list always goes to the log; --list also prints it
        flag = " [keep]" if keep.matches(e.path) else ""
        line = f"  {e.size:>10}  {e.path}{flag}"
        out(line) if list_files else detail(out, line)
    if dry_run and not list_files:
        out("  (full file list is in the log; add --list to print it)")
    if dry_run:
        pending = pending_uploads(cfg, manifest, out)
        if pending is None:
            out("Dry run: nothing published.")
        elif not pending:
            out("Dry run: everything is already published (friends would see 'Up to date').")
        else:
            seen: set[str] = set()
            uniq = [e for e in pending if not (e.sha256 in seen or seen.add(e.sha256))]
            out(f"Dry run: would upload {len(uniq)} new file(s), {fmt_size(sum(e.size for e in uniq))}:")
            for e in pending[:40]:
                out(f"    {e.path}")
            if len(pending) > 40:
                out(f"    … and {len(pending) - 40} more (see the log)")
                for e in pending[40:]:
                    detail(out, f"    {e.path}")
    else:
        out(f"Publishing → {cfg.dest}")
        if repo:
            _publish_github(repo, manifest, sources, prune, out)
        elif is_remote(cfg.dest):
            _publish_remote(cfg.dest, cfg, manifest, sources, prune, out)
        else:
            _publish_local(Path(cfg.dest).expanduser(), manifest, sources, prune, out)
        out("Published.")
    if push_server if push_server is not None else cfg.server.enabled:
        push_to_server(cfg, dry_run, out)
    return manifest


def report_mod_updates(cfg: PublishConfig, out: Callable[[str], None], quiet: bool = False) -> int:
    """Print mods with newer versions on Thunderstore/Hexium. Never fails a publish."""
    from .http import HttpFetcher
    from .modupdates import check, installed_mods

    try:
        mods = installed_mods(cfg.source)
        updates = check(mods, HttpFetcher(timeout=8).get_bytes)
    except Exception as e:  # noqa: BLE001
        if not quiet:
            out(f"Couldn't check for mod updates: {e}")
        return 0
    if not updates:
        if not quiet:
            out(f"All {len(mods)} mods are up to date on Thunderstore/Hexium.")
        return 0
    if quiet:
        out("")
    out(f"{len(updates)} mod update(s) available (update in Gale, then publish):")
    for u in updates:
        out(f"  {u.line()}  {u.url}")
    return len(updates)


def default_config_path() -> Path:
    local = Path("publish.toml")
    if local.is_file():
        return local
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lynx-mod-launcher" / "publish.toml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lynx-publish", description="Publish a Gale profile for Lynx Mod Launcher.")
    ap.add_argument("-c", "--config", type=Path, help="settings file (default: ./publish.toml or ~/.config/lynx-mod-launcher/publish.toml)")
    ap.add_argument("-p", "--profile", help="Gale profile name to publish (overrides the settings file)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would be published/copied; change nothing")
    ap.add_argument("-l", "--list", action="store_true", help="list every published file")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--server", dest="server", action="store_true", default=None, help="also push to the game server")
    grp.add_argument("--no-server", dest="server", action="store_false", help="skip the game server push")
    grp.add_argument("--server-only", action="store_true", help="only push to the game server; don't publish for friends")
    ap.add_argument("--confirm", action="store_true", help="do a dry run first, then ask before doing it for real")
    ap.add_argument("--prune", action="store_true", help="delete objects no longer referenced (run when nobody is mid-update)")
    ap.add_argument("--check-updates", action="store_true", help="only list mods with newer versions on Thunderstore/Hexium")
    rg = ap.add_mutually_exclusive_group()
    rg.add_argument("--restart-when-empty", action="store_true",
                    help="restart the server once nobody is on (in the background; needs [server.restart])")
    rg.add_argument("--no-restart", action="store_true", help="don't offer to restart the server afterwards")
    rg.add_argument("--restart-status", action="store_true", help="show whether a restart is waiting")
    rg.add_argument("--restart-cancel", action="store_true", help="cancel a waiting restart")
    rg.add_argument("--restart-wait", action="store_true", help=argparse.SUPPRESS)  # the background job itself
    args = ap.parse_args(argv)
    log = PublishLog()
    log.detail(f"lynx-publish {' '.join(argv if argv is not None else sys.argv[1:])}  ({datetime.now().isoformat(timespec='seconds')})")

    from . import restarter

    if args.restart_status:
        print(restarter.status_text())
        return 0
    if args.restart_cancel:
        print("Cancelled the waiting restart." if restarter.cancel() else "No restart is waiting.")
        return 0

    def run(dry: bool) -> int:
        """Returns the number of server changes."""
        do_server = args.server_only or (args.server if args.server is not None else cfg.server.enabled)
        if not args.server_only:
            publish(cfg, dry_run=dry, list_files=args.list, push_server=False, prune=args.prune, out=log)
        if not do_server:
            return 0
        if not cfg.server.dest:
            raise PublishError("[server] dest is not set in your settings")
        check_dest(cfg.server.dest)
        return push_to_server(cfg, dry, log)

    def offer_restart(changes: int) -> None:
        rc = cfg.server.restart
        if rc is None or args.no_restart or args.dry_run:
            return
        if not args.restart_when_empty:
            if not changes or not sys.stdin.isatty():
                return
            try:
                answer = input("\nRestart the server once nobody's on? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            log.detail(f"restart answer: {answer!r}")
            if answer not in ("y", "yes"):
                log("Not restarting; do it from the panel when it suits you.")
                return
        try:
            log(f"Server now: {restarter.preflight(rc)}")
            pid = restarter.start_background(config_path, args.profile)
        except restarter.RestartError as e:
            log(f"Couldn't set up the restart: {e}")
            return
        log(f"Waiting in the background (pid {pid}); checks every {rc.poll_minutes:g} min, "
            f"gives up after {rc.give_up_hours:g} h.")
        log("  lynx-publish --restart-status   see how it's going")
        log("  lynx-publish --restart-cancel   call it off")

    try:
        config_path = (args.config or default_config_path()).resolve()
        cfg = load_config(config_path, args.profile)
        if args.restart_wait:
            log.close()
            log.path.unlink(missing_ok=True)  # the job writes its own log
            return _restart_wait(cfg)
        if args.restart_when_empty and not (args.server_only or args.server):
            if cfg.server.restart is None:
                raise PublishError("--restart-when-empty needs a [server.restart] section in your settings")
            offer_restart(0)
            print(f"Full log: {log.path}")
            log.close()
            return 0
        if args.check_updates:
            report_mod_updates(cfg, log)
            print(f"Full log: {log.path}")
            log.close()
            return 0
        if args.confirm and not args.dry_run:
            run(True)
            log("")
            try:
                answer = input("Go ahead and do this for real? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            log.detail(f"answer: {answer!r}")
            if answer not in ("y", "yes"):
                log("Cancelled; nothing changed.")
                print(f"Full log: {log.path}")
                log.close()
                return 0
            log("")
        changes = run(args.dry_run)
        offer_restart(changes)
        if not args.server_only:
            report_mod_updates(cfg, log, quiet=True)  # a heads-up only; silent when nothing's new
    except PublishError as e:
        log.detail(f"error: {e}")
        print(f"error: {e}\nNothing was published.", file=sys.stderr)
        print(f"Full log: {log.path}", file=sys.stderr)
        log.close()
        return 1
    except KeyboardInterrupt:
        log("Interrupted.")
        log.close()
        return 130
    print(f"Full log: {log.path}")
    log.close()
    return 0


def _restart_wait(cfg: PublishConfig) -> int:
    """The background job started by --restart-when-empty or the prompt."""
    import signal

    from . import restarter

    def out(msg: str) -> None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)

    def on_term(_sig, _frame):
        out("Cancelled; not restarting.")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    try:
        if cfg.server.restart is None:
            out("error: no [server.restart] settings")
            return 1
        restarter.wait_and_restart(cfg.server.restart, out)
        return 0
    except restarter.RestartError as e:
        out(f"error: {e}")
        return 1
    finally:
        try:
            if json.loads(restarter.job_path().read_text("utf-8")).get("pid") == os.getpid():
                restarter.job_path().unlink()
        except (OSError, ValueError, AttributeError):
            pass


if __name__ == "__main__":
    sys.exit(main())
