"""Client entry point: settings, logging, sync + launch orchestration, command line."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import APP_NAME, __version__, build_config
from . import doorstop, steam
from .http import HttpFetcher
from .paths import user_data_dir
from .sync import Fetcher, Progress, ProgressFn, Syncer, SyncResult

log = logging.getLogger("lynxlauncher")


@dataclass
class Settings:
    game_dir: str = ""
    launch_mode: str = "auto"  # auto | steam | direct (direct = run valheim.exe without Steam -applaunch; Windows only)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            raw = json.loads(path.read_text("utf-8"))
            known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__ and isinstance(v, str)}
            s = cls(**known)
        except (OSError, ValueError, TypeError):
            s = cls()
        s._path = path  # type: ignore[attr-defined]
        return s

    def save(self) -> None:
        path: Path = self._path  # type: ignore[attr-defined]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), "utf-8")


def setup_logging(data_dir: Path, verbose: bool = False) -> Path:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "launcher.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)
    if sys.stderr is not None:  # None in a windowed PyInstaller build
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
        root.addHandler(sh)
    return log_path


class LauncherCore:
    def __init__(
        self,
        manifest_url: str,
        data_dir: Path | None = None,
        profile_dir: Path | None = None,
        game_dir: Path | None = None,
        fetcher: Fetcher | None = None,
    ):
        self.manifest_url = manifest_url
        self.data_dir = Path(data_dir) if data_dir else user_data_dir()
        self.profile_dir = Path(profile_dir) if profile_dir else self.data_dir / "profile"
        self.settings = Settings.load(self.data_dir / "settings.json")
        self._game_override = Path(game_dir) if game_dir else None
        self.fetcher = fetcher or HttpFetcher()

    # -- game folder -------------------------------------------------------

    def find_game(self) -> Path | None:
        if self._game_override:
            return self._game_override if steam.is_valheim_dir(self._game_override) else None
        if self.settings.game_dir and steam.is_valheim_dir(Path(self.settings.game_dir)):
            return Path(self.settings.game_dir)
        found = steam.find_valheim()
        if found:
            self.settings.game_dir = str(found)
            self.settings.save()
        return found

    def set_game_dir(self, path: str | Path) -> bool:
        p = Path(path)
        if not steam.is_valheim_dir(p):
            return False
        self.settings.game_dir = str(p)
        self.settings.save()
        return True

    # -- sync --------------------------------------------------------------

    def sync(self, progress: ProgressFn | None = None) -> SyncResult:
        log.info("%s %s, profile %s, manifest %s", APP_NAME, __version__, self.profile_dir, self.manifest_url)
        result = Syncer(self.profile_dir, self.fetcher).run(self.manifest_url, progress or (lambda p: None))
        log.info("sync result: %s / %s", result.headline(), result.detail())
        return result

    def can_launch(self) -> bool:
        return self.profile_dir.joinpath(*doorstop.PRELOADER_REL).is_file()

    # -- launch ------------------------------------------------------------

    def prepare_launch(self) -> doorstop.LaunchSpec:
        game = self.find_game()
        if game is None:
            raise doorstop.DoorstopError("Couldn't find Valheim. Pick the Valheim folder first.")
        info = doorstop.detect_doorstop(self.profile_dir)
        kind = doorstop.detect_game_kind(game)
        log.info("game %s (%s), doorstop v%d", game, kind.value, info.version)
        doorstop.prepare_game_dir(game, info, kind)
        root = steam.steam_root_for_game(game)
        cmd = steam.steam_command(root)
        mode = self.settings.launch_mode
        spec = doorstop.build_launch(kind, game, info, cmd, mode)
        if kind is doorstop.GameKind.LINUX_PROTON and root is not None:
            opts = steam.launch_options(root)
            if not any("winhttp" in o.lower() for o in opts):
                spec.warnings.append(
                    "Valheim is running through Proton, so Steam needs one launch option for the mods to load.\n\n"
                    "In Steam: right-click Valheim → Properties → Launch Options, and paste:\n\n"
                    f"{doorstop.PROTON_LAUNCH_OPTIONS}\n\n"
                    "(It has been copied to your clipboard.) You only have to do this once."
                )
        return spec

    def launch(self, spec: doorstop.LaunchSpec) -> subprocess.Popen:
        return doorstop.start(spec)


def _cli_progress(p: Progress) -> None:
    if p.phase == "download" and p.total:
        pct = 100 * p.done // p.total
        print(f"\r  downloading {pct:3d}%", end="", flush=True)
    elif p.phase == "apply" and p.done == 0:
        print("\n  installing…", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="LynxModLauncher", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--manifest-url", help="override the built-in manifest URL")
    ap.add_argument("--game-dir", type=Path, help="Valheim folder (skips Steam detection)")
    ap.add_argument("--data-dir", type=Path, help="launcher data folder (settings, logs, profile)")
    ap.add_argument("--profile-dir", type=Path, help="mod profile folder (default: <data-dir>/profile)")
    ap.add_argument("--sync-only", action="store_true", help="update the mods without a window and exit")
    ap.add_argument("--headless", action="store_true", help="update and launch without a window")
    ap.add_argument("--print-launch", action="store_true", help="with --headless: print the launch command instead of running it")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    url = args.manifest_url or os.environ.get("LYNX_MANIFEST_URL") or build_config.MANIFEST_URL
    data_dir = args.data_dir or user_data_dir()
    log_path = setup_logging(data_dir, args.verbose)
    core = LauncherCore(url, data_dir, args.profile_dir, args.game_dir)

    if not (args.sync_only or args.headless):
        from .gui import run_gui

        return run_gui(core, log_path)

    result = core.sync(_cli_progress)
    print(f"\n{result.headline()}. {result.detail()}".strip())
    if args.sync_only:
        return 0 if result.ok else 1
    if not core.can_launch():
        print("Nothing to launch yet.")
        return 1
    try:
        spec = core.prepare_launch()
    except doorstop.DoorstopError as e:
        print(f"Can't launch: {e}")
        return 1
    for w in spec.warnings:
        print(f"\nNOTE: {w}\n")
    if args.print_launch:
        print(subprocess.list2cmdline(spec.argv))
        for k in sorted(k for k, v in spec.env.items() if os.environ.get(k) != v):
            print(f"  {k}={spec.env[k]}")
        return 0
    core.launch(spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
