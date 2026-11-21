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


# Dark theme. The splash frames are pre-rendered onto BG, so BG must match them exactly.
BG = "#14181c"
FG = "#e8ece6"
MUTED = "#8b949b"
GREEN = "#44ab38"
GREEN_HI = "#55c447"
TROUGH = "#262d33"
BTN = "#242b31"
BTN_HI = "#2f373f"
BORDER = "#2c343b"
FRAME_MS = 45


def load_image(name: str):
    try:
        return tk.PhotoImage(file=str(ASSETS / name))
    except (tk.TclError, OSError) as e:
        log.debug("couldn't load %s: %s", name, e)
        return None


def apply_theme(root: tk.Tk) -> None:
    root.configure(bg=BG)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=FG, bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                troughcolor=TROUGH, focuscolor=BG)
    s.configure("TFrame", background=BG)
    s.configure("TLabel", background=BG, foreground=FG)
    s.configure("Muted.TLabel", foreground=MUTED)
    s.configure("Link.TLabel", foreground=GREEN_HI)
    s.configure("TButton", background=BTN, foreground=FG, bordercolor=BORDER, lightcolor=BTN, darkcolor=BTN,
                padding=(14, 6), relief="flat")
    s.map("TButton", background=[("disabled", BG), ("pressed", BTN_HI), ("active", BTN_HI)],
          foreground=[("disabled", "#59626a")], bordercolor=[("disabled", "#20262b")])
    s.configure("Accent.TButton", background=GREEN, foreground="#0c1a0a", bordercolor=GREEN,
                lightcolor=GREEN, darkcolor=GREEN, font=("TkDefaultFont", 10, "bold"))
    s.map("Accent.TButton", background=[("disabled", "#243322"), ("pressed", "#3a9430"), ("active", GREEN_HI)],
          foreground=[("disabled", "#4f6a4b")], bordercolor=[("disabled", "#243322")])
    s.configure("Horizontal.TProgressbar", background=GREEN, troughcolor=TROUGH, bordercolor=TROUGH,
                lightcolor=GREEN, darkcolor=GREEN, thickness=8)


def center_over(win: tk.Misc, w: int, h: int, ref: tk.Misc) -> None:
    """Center `win` over `ref` (the main window), so it lands on the monitor the user is
    looking at. Tk's screen size is the whole multi-monitor desktop, so centering on
    the "screen" would put it between monitors."""
    rx, ry = ref.winfo_rootx(), ref.winfo_rooty()
    rw, rh = ref.winfo_width(), ref.winfo_height()
    if rw <= 1:  # not mapped yet: fall back to the pointer's position
        rx, ry, rw, rh = ref.winfo_pointerx(), ref.winfo_pointery(), 0, 0
    x = max(0, rx + (rw - w) // 2)
    y = max(0, ry + (rh - h) // 2)
    win.geometry(f"{w}x{h}+{x}+{y}")


class Splash:
    """Borderless, centered splash with the animated lynx. Drag anywhere to move it."""

    def __init__(self, parent: tk.Tk, on_open_log, on_close):
        self.top = top = tk.Toplevel(parent, bg=BORDER)
        top.overrideredirect(True)
        top.title("Starting Valheim")
        inner = tk.Frame(top, bg=BG)
        inner.pack(padx=1, pady=1, fill="both", expand=True)
        self.frames = [img for i in range(60) if (img := load_image(f"splash/f{i:02d}.png"))]
        self.frame_i = 0
        close = tk.Label(inner, text="✕", bg=BG, fg=MUTED, font=("TkDefaultFont", 11), cursor="hand2")
        close.place(relx=1.0, x=-12, y=8, anchor="ne")
        close.bind("<Button-1>", lambda _e: on_close())
        self.logo = tk.Label(inner, bg=BG, bd=0, image=self.frames[0] if self.frames else "")
        self.logo.pack(pady=(26, 6))
        tk.Label(inner, text="Starting Valheim", bg=BG, fg=FG, font=("TkDefaultFont", 16, "bold")).pack()
        self.status = tk.StringVar(value="Waiting for Steam to start Valheim…")
        tk.Label(inner, textvariable=self.status, bg=BG, fg=FG, font=("TkDefaultFont", 11)).pack(pady=(8, 8))
        self.bar = ttk.Progressbar(inner, length=300, mode="indeterminate", maximum=1000)
        self.bar.pack()
        self.bar.start(14)
        self.detail = tk.StringVar(value="")
        tk.Label(inner, textvariable=self.detail, bg=BG, fg=MUTED, wraplength=320, justify="center",
                 font=("TkDefaultFont", 9)).pack(pady=(8, 4))
        link = tk.Label(inner, text="Open game log", bg=BG, fg=MUTED, cursor="hand2", font=("TkDefaultFont", 9, "underline"))
        link.pack(pady=(4, 16))
        link.bind("<Button-1>", lambda _e: on_open_log())
        for w in (inner, self.logo):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
        top.bind("<Escape>", lambda _e: on_close())
        center_over(top, 380, 480, parent)
        top.lift()
        self._animate()

    def _animate(self) -> None:
        if self.frames:
            self.frame_i = (self.frame_i + 1) % len(self.frames)
            self.logo.configure(image=self.frames[self.frame_i])
        self.top.after(FRAME_MS, self._animate)

    def _drag_start(self, e) -> None:
        self._dx, self._dy = e.x_root - self.top.winfo_x(), e.y_root - self.top.winfo_y()

    def _drag(self, e) -> None:
        self.top.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def determinate(self, fraction: float) -> None:
        if str(self.bar["mode"]) != "determinate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        self.bar.configure(value=max(0, min(1000, 1000 * fraction)))

    def stop(self) -> None:
        self.bar.stop()


class LauncherWindow:
    def __init__(self, core: LauncherCore, log_path: Path, argv: list[str] | None = None):
        self.core = core
        self.argv = argv or []
        self.log_path = log_path
        self.events: queue.Queue = queue.Queue()
        self.busy = False

        self.root = tk.Tk()
        apply_theme(self.root)
        self.root.title(APP_NAME)
        self._icon = load_image("icon.png")
        if self._icon:
            self.root.iconphoto(True, self._icon)
        self._header_img = load_image("header.png")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        frm = ttk.Frame(self.root, padding=18)
        frm.grid(sticky="nsew")
        self.main_frame = frm
        ttk.Label(frm, text=" Lynx Mod Launcher", image=self._header_img or "", compound="left",
                  font=("TkDefaultFont", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, text=f"v{__version__}", style="Muted.TLabel").grid(row=0, column=2, sticky="ne")

        self.status = tk.StringVar(value="Starting…")
        ttk.Label(frm, textvariable=self.status, font=("TkDefaultFont", 11)).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(10, 4))
        self.bar = ttk.Progressbar(frm, length=420, mode="determinate", maximum=1000)
        self.bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.detail = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.detail, style="Muted.TLabel", wraplength=420, justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 12))

        self.logs_btn = ttk.Button(frm, text="Open logs", command=self.open_logs)
        self.logs_btn.grid(row=4, column=0, sticky="w")
        self.retry_btn = ttk.Button(frm, text="Check again", command=self.start_sync, state="disabled")
        self.retry_btn.grid(row=4, column=1, sticky="e", padx=8)
        self.launch_btn = ttk.Button(frm, text="Launch", style="Accent.TButton", command=self.on_launch, state="disabled")
        self.launch_btn.grid(row=4, column=2, sticky="e")
        frm.columnconfigure(1, weight=1)
        # Only on the publisher's PC: "N mod updates available in Gale" (hidden otherwise).
        self.mod_updates: list = []
        self.updates_text = tk.StringVar(value="")
        self.updates_lbl = ttk.Label(frm, textvariable=self.updates_text, style="Link.TLabel",
                                     wraplength=420, justify="left", cursor="hand2")
        self.updates_lbl.grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.updates_lbl.grid_remove()
        self.updates_lbl.bind("<Button-1>", lambda _e: self.show_mod_updates())
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
                elif kind == "mod_updates":
                    self._on_mod_updates(value)
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

    # -- mod updates (publisher's PC only) ------------------------------------

    def start_mod_update_check(self) -> None:
        cfg_path = publisher_config_path()
        if not cfg_path.is_file():
            return  # a friend's PC: Michael's Gale isn't here

        def work() -> None:
            try:
                from .http import HttpFetcher
                from .modupdates import check, installed_mods
                from .publisher import load_config

                cfg = load_config(cfg_path)
                updates = check(installed_mods(cfg.source), HttpFetcher(timeout=8).get_bytes)
                log.info("mod update check: %d update(s)%s", len(updates),
                         "".join(f"\n  {u.line()}" for u in updates))
                self.events.put(("mod_updates", updates))
            except Exception:  # noqa: BLE001 - only a hint; never get in the way
                log.warning("mod update check failed", exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def _on_mod_updates(self, updates: list) -> None:
        from .modupdates import summary

        self.mod_updates = updates
        if updates:
            self.updates_text.set("⬆ " + summary(updates) + " (click for details)")
            self.updates_lbl.grid()
        else:
            self.updates_lbl.grid_remove()

    def show_mod_updates(self) -> None:
        lines = "\n".join(u.line() for u in self.mod_updates)
        messagebox.showinfo(APP_NAME, f"Newer versions are out:\n\n{lines}\n\n"
                                      "Update them in Gale, then publish as usual.", parent=self.root)

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
        self.splash = Splash(self.root, self.open_game_log, self.root.destroy)
        self.root.withdraw()
        self.watcher = LaunchWatcher(self.core.game_log)
        self.sp_started = time.monotonic()
        self.sp_last_change = (self.sp_started, -1)
        try:
            self.sp_expected = int(self.core.settings.last_mod_count or 0)
        except ValueError:
            self.sp_expected = 0
        self.root.after(500, self.poll_splash)

    def poll_splash(self) -> None:
        sp = self.splash
        st = self.watcher.poll()
        now = time.monotonic()
        elapsed = now - self.sp_started
        progress = (st.loaded, st.mod_sources, st.last_mod)
        if progress != self.sp_last_change[1]:
            self.sp_last_change = (now, progress)
        if st.phase == "ready":
            sp.determinate(1.0)
            sp.status.set("Mods loaded. Valheim is opening!")
            count = st.final or st.loaded
            done = f"{count} mods loaded" if count else "All mods loaded"
            sp.detail.set(done + (f", {len(st.errors)} errors in the game log" if st.errors else ""))
            if count:
                self.core.settings.last_mod_count = str(count)
                try:
                    self.core.settings.save()
                except OSError:
                    pass
            log.info("Valheim started: %d mods loaded in %.0fs (%d Loading lines seen)", count, elapsed, st.loaded)
            self.root.after(4000, self.root.destroy)
            return
        if st.phase == "loading" and not st.loaded:
            # This profile doesn't write Info lines to disk, so there's no exact count: show the
            # mod that's talking most recently instead.
            sp.status.set("Loading mods…")
            sp.detail.set(st.last_mod)
        elif st.phase == "loading":
            expected = st.total or self.sp_expected  # this launch's count, else last launch's
            if expected:
                sp.determinate(min(0.99, st.loaded / expected))
                sp.status.set(f"Loading mods… {st.loaded} of {expected}")
            else:
                sp.status.set(f"Loading mods… {st.loaded}")
            detail = st.last_mod
            if now - self.sp_last_change[0] > STALL_WARN_SECONDS:
                detail = f"Still on {st.last_mod}… this one is slow. 'Open game log' shows what BepInEx is doing."
            sp.detail.set(detail)
        elif st.phase == "starting":
            sp.status.set("Valheim is starting, loading BepInEx…")
            sp.detail.set("")
        elif elapsed > WAIT_WARN_SECONDS:
            sp.status.set("Valheim hasn't started yet.")
            sp.detail.set("Make sure Steam is running and signed in. It can also just be slow. "
                          "This window keeps watching.")
        if elapsed > GIVE_UP_SECONDS:
            sp.stop()
            sp.status.set("Stopped watching. Valheim may still be starting.")
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
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.minsize(w, h)  # placement is left to the window manager (right monitor)
        self.root.after(250, self.start_sync)
        self.root.after(1500, self.start_mod_update_check)
        self.root.after(80, self.poll)
        self.root.mainloop()
        return 0


def publisher_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lynx-mod-launcher" / "publish.toml"


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
