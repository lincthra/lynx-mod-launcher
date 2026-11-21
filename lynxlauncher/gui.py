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

from . import APP_NAME, __version__
from .doorstop import PROTON_LAUNCH_OPTIONS, DoorstopError
from .launcher_app import LauncherCore
from .paths import fmt_size
from .sync import Progress, SyncResult

log = logging.getLogger(__name__)


class LauncherWindow:
    def __init__(self, core: LauncherCore, log_path: Path):
        self.core = core
        self.log_path = log_path
        self.events: queue.Queue = queue.Queue()
        self.busy = False

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        frm = ttk.Frame(self.root, padding=18)
        frm.grid(sticky="nsew")
        ttk.Label(frm, text="Lynx Mod Launcher", font=("TkDefaultFont", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
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
        self.status.set("Starting Valheim…")
        self.launch_btn.state(["disabled"])
        self.root.after(2500, self.root.destroy)

    def open_logs(self) -> None:
        folder = str(self.log_path.parent)
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


def run_gui(core: LauncherCore, log_path: Path) -> int:
    return LauncherWindow(core, log_path).run()
