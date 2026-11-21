"""The tkinter window: status, progress bar, Launch button."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__, selfupdate
from .doorstop import PROTON_LAUNCH_OPTIONS, DoorstopError
from .launcher_app import LauncherCore
from .launchwatch import LaunchWatcher
from .paths import fmt_size
from .sync import Progress, SyncResult

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent / "assets"

WAIT_WARN_SECONDS = 90       # nothing in the BepInEx log by now: say so
STALL_WARN_SECONDS = 180     # mod count hasn't moved for this long
GIVE_UP_SECONDS = 600        # stop watching (window stays open)


def load_image(name: str):
    try:
        return tk.PhotoImage(file=str(ASSETS / name))
    except (tk.TclError, OSError) as e:
        log.debug("couldn't load %s: %s", name, e)
        return None


class LauncherWindow:
    def __init__(self, core: LauncherCore, log_path: Path, argv: list[str] | None = None):
        self.core = core
        self.argv = argv or []
        self.log_path = log_path
        self.events: queue.Queue = queue.Queue()
        self.busy = False

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self._icon = load_image("icon.png")
        if self._icon:
            self.root.iconphoto(True, self._icon)
        self._header_img = load_image("header.png")
        self._splash_img = load_image("splash.png")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        frm = ttk.Frame(self.root, padding=18)
        frm.grid(sticky="nsew")
        self.main_frame = frm
        ttk.Label(frm, text=" Lynx Mod Launcher", image=self._header_img or "", compound="left",
                  font=("TkDefaultFont", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, text=f"v{__version__}", foreground="gray").grid(row=0, column=2, sticky="e")

        self.status = tk.StringVar(value="Starting…")
        ttk.Label(frm, textvariable=self.status, font=("TkDefaultFont", 11)).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(10, 4))
        self.bar = ttk.Progressbar(frm, length=420, mode="determinate", maximum=1000)
        self.bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.detail = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.detail, foreground="gray", wraplength=420, justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 12))

        self.logs_btn = ttk.Button(frm, text="Open logs", command=self.open_logs)
        self.logs_btn.grid(row=4, column=0, sticky="w")
        self.retry_btn = ttk.Button(frm, text="Check again", command=self.start_sync, state="disabled")
        self.retry_btn.grid(row=4, column=1, sticky="e", padx=8)
        self.launch_btn = ttk.Button(frm, text="Launch", command=self.on_launch, state="disabled")
        self.launch_btn.grid(row=4, column=2, sticky="e")
        frm.columnconfigure(1, weight=1)
        self.root.bind("<Return>", lambda _e: self.on_launch() if str(self.launch_btn["state"]) == "normal" else None)

    # -- sync in a background thread --------------------------------------

    def start_sync(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.launch_btn.state(["disabled"])
        self.retry_btn.state(["disabled"])
        self.bar.configure(value=0)
        self.detail.set("")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        if not getattr(self, "_update_checked", False):
            self._update_checked = True
            info = self.core.check_self_update()
            if info:
                self.events.put(("updating", info.version))
                try:
                    exe = self.core.apply_self_update(info, lambda d, t: self.events.put(("selfprogress", (d, t))))
                    self.events.put(("relaunch", exe))
                    return
                except Exception as e:  # noqa: BLE001
                    log.exception("self-update failed")
                    self.events.put(("update_failed", str(e)))
        try:
            result = self.core.sync(throttled(lambda p: self.events.put(("progress", p))))
        except Exception as e:  # noqa: BLE001
            log.exception("sync crashed")
            result = SyncResult(error=f"Unexpected error: {e}")
        self.events.put(("done", result))

    def poll(self) -> None:
        # Drain everything queued, but only draw the newest progress, so a fast download
        # can't keep the window too busy to lay itself out or repaint.
        latest: Progress | None = None
        done: SyncResult | None = None
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    latest = value
                elif kind == "updating":
                    self.status.set(f"Updating the launcher to v{value}…")
                elif kind == "selfprogress":
                    d, t = value
                    self.bar.configure(value=1000 * d / t if t else 0)
                elif kind == "update_failed":
                    self.detail.set(f"Launcher update failed ({value}); continuing with this version.")
                elif kind == "relaunch":
                    self.status.set("Restarting the launcher…")
                    try:
                        selfupdate.relaunch(value, self.argv)
                    except OSError as e:
                        log.error("relaunch failed: %s", e)
                        messagebox.showinfo(APP_NAME, "The launcher was updated. Please start it again.", parent=self.root)
                    self.root.after(300, self.root.destroy)
                    return
                else:
                    done = value
        except queue.Empty:
            pass
        if latest is not None:
            self._on_progress(latest)
        if done is not None:
            self._on_done(done)
        self.root.after(80, self.poll)

    def _on_progress(self, p: Progress) -> None:
        if p.phase == "manifest":
            self.status.set("Checking for updates…")
        elif p.phase == "scan":
            self.status.set("Checking your files…")
            self.bar.configure(value=1000 * p.done / p.total if p.total else 0)
        elif p.phase == "download":
            self.status.set(f"Downloading… {fmt_size(p.done)} of {fmt_size(p.total)}")
            self.bar.configure(value=1000 * p.done / p.total if p.total else 1000)
        elif p.phase == "apply":
            self.status.set("Installing…")
            self.bar.configure(value=1000 * p.done / p.total if p.total else 1000)

    def _on_done(self, r: SyncResult) -> None:
        self.busy = False
        self.bar.configure(value=1000)
        self.status.set(r.headline())
        detail = r.detail()
        if self.core.can_launch():
            self.launch_btn.state(["!disabled"])
            self.launch_btn.focus_set()
            if r.error and "still launch" not in detail:
                detail += " You can still launch with the mods you already have."
        else:
            detail = (detail + "\n" if detail else "") + "The mods haven't been downloaded yet, so there's nothing to launch."
        self.detail.set(detail)
        self.retry_btn.state(["!disabled"])

    # -- launch --------------------------------------------------------------

    def ensure_game(self) -> bool:
        if self.core.find_game():
            return True
        messagebox.showinfo(APP_NAME, "Couldn't find Valheim automatically.\n\nPlease pick your Valheim folder "
                                      "(the one that contains valheim.exe).", parent=self.root)
        while True:
            picked = filedialog.askdirectory(title="Select your Valheim folder", parent=self.root)
            if not picked:
                return False
            if self.core.set_game_dir(picked):
                return True
            messagebox.showerror(APP_NAME, "That folder doesn't contain Valheim. Try again.", parent=self.root)

    def on_launch(self) -> None:
        if self.busy or not self.ensure_game():
            return
        try:
            spec = self.core.prepare_launch()
        except (DoorstopError, OSError) as e:
            log.error("can't launch: %s", e)
            messagebox.showerror(APP_NAME, str(e), parent=self.root)
            return
        for warning in spec.warnings:
            if PROTON_LAUNCH_OPTIONS in warning:
                self.root.clipboard_clear()
                self.root.clipboard_append(PROTON_LAUNCH_OPTIONS)
            if not messagebox.askokcancel(APP_NAME, warning + "\n\nLaunch now?", parent=self.root):
                return
        try:
            self.core.launch(spec)
        except OSError as e:
            log.exception("launch failed")
            messagebox.showerror(APP_NAME, f"Couldn't start Valheim: {e}", parent=self.root)
            return
        self.show_splash()

    # -- splash while Valheim starts ------------------------------------------

    def show_splash(self) -> None:
        self.main_frame.grid_remove()
        self.root.minsize(1, 1)
        sp = ttk.Frame(self.root, padding=(28, 20))
        sp.grid(sticky="nsew")
        if self._splash_img:
            ttk.Label(sp, image=self._splash_img).grid(row=0, column=0, columnspan=2, pady=(0, 12))
        ttk.Label(sp, text="Starting Valheim", font=("TkDefaultFont", 16, "bold")).grid(row=1, column=0, columnspan=2)
        self.sp_status = tk.StringVar(value="Waiting for Steam to start Valheim…")
        ttk.Label(sp, textvariable=self.sp_status, font=("TkDefaultFont", 11)).grid(row=2, column=0, columnspan=2, pady=(6, 4))
        self.sp_bar = ttk.Progressbar(sp, length=320, mode="indeterminate", maximum=1000)
        self.sp_bar.grid(row=3, column=0, columnspan=2, pady=4)
        self.sp_bar.start(15)
        self.sp_detail = tk.StringVar(value="")
        ttk.Label(sp, textvariable=self.sp_detail, foreground="gray", wraplength=340, justify="center").grid(
            row=4, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(sp, text="Open game log", command=self.open_game_log).grid(row=5, column=0, sticky="w")
        ttk.Button(sp, text="Close", command=self.root.destroy).grid(row=5, column=1, sticky="e")
        sp.columnconfigure(0, weight=1)
        self.root.title("Starting Valheim – " + APP_NAME)
        self.watcher = LaunchWatcher(self.core.game_log)
        self.sp_started = time.monotonic()
        self.sp_last_change = (self.sp_started, -1)
        try:
            self.sp_expected = int(self.core.settings.last_mod_count or 0)
        except ValueError:
            self.sp_expected = 0
        self.root.after(500, self.poll_splash)

    def poll_splash(self) -> None:
        st = self.watcher.poll()
        now = time.monotonic()
        elapsed = now - self.sp_started
        if st.loaded != self.sp_last_change[1]:
            self.sp_last_change = (now, st.loaded)
        if st.phase == "ready":
            self.sp_bar.stop()
            self.sp_bar.configure(mode="determinate", value=1000)
            self.sp_status.set("Mods loaded. Valheim is opening!")
            self.sp_detail.set(f"{st.loaded} mods loaded" + (f", {len(st.errors)} errors in the game log" if st.errors else ""))
            if st.loaded:
                self.core.settings.last_mod_count = str(st.loaded)
                try:
                    self.core.settings.save()
                except OSError:
                    pass
            log.info("Valheim started: %d mods loaded in %.0fs", st.loaded, elapsed)
            self.root.after(4000, self.root.destroy)
            return
        if st.phase == "loading":
            if self.sp_expected:
                if str(self.sp_bar["mode"]) != "determinate":
                    self.sp_bar.stop()
                    self.sp_bar.configure(mode="determinate")
                self.sp_bar.configure(value=min(990, 1000 * st.loaded / self.sp_expected))
                self.sp_status.set(f"Loading mods… {st.loaded} of {self.sp_expected}")
            else:
                self.sp_status.set(f"Loading mods… {st.loaded}")
            detail = st.last_mod
            if now - self.sp_last_change[0] > STALL_WARN_SECONDS:
                detail = f"Still on {st.last_mod}… this one is slow. 'Open game log' shows what BepInEx is doing."
            self.sp_detail.set(detail)
        elif st.phase == "starting":
            self.sp_status.set("Valheim is starting, loading BepInEx…")
            self.sp_detail.set("")
        elif elapsed > WAIT_WARN_SECONDS:
            self.sp_status.set("Valheim hasn't started yet.")
            self.sp_detail.set("Make sure Steam is running and signed in. It can also just be slow. "
                               "This window keeps watching.")
        if elapsed > GIVE_UP_SECONDS:
            self.sp_bar.stop()
            self.sp_status.set("Stopped watching. Valheim may still be starting.")
            return
        self.root.after(500, self.poll_splash)

    def open_game_log(self) -> None:
        self._open_folder(self.core.game_log.parent)

    def open_logs(self) -> None:
        self._open_folder(self.log_path.parent)

    def _open_folder(self, path: Path) -> None:
        folder = str(path)
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", folder], start_new_session=True)
        except OSError:
            messagebox.showinfo(APP_NAME, f"Logs are in:\n{folder}", parent=self.root)

    def on_close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_NAME, "An update is in progress. Quit anyway?", parent=self.root):
            return
        self.root.destroy()

    def run(self) -> int:
        # Settle the window's full size before any work starts, and don't let it shrink.
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())
        self.root.after(250, self.start_sync)
        self.root.after(80, self.poll)
        self.root.mainloop()
        return 0


def throttled(fn, interval: float = 0.1):
    """Pass progress on at most every `interval` seconds (plus every phase change)."""
    last = {"t": 0.0, "phase": None}

    def wrapper(p: Progress) -> None:
        now = time.monotonic()
        if p.phase != last["phase"] or now - last["t"] >= interval or (p.total and p.done >= p.total):
            last["t"], last["phase"] = now, p.phase
            fn(p)

    return wrapper


def run_gui(core: LauncherCore, log_path: Path, argv: list[str] | None = None) -> int:
    return LauncherWindow(core, log_path, argv).run()
