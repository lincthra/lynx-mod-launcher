import os
import time

from lynxlauncher.launchwatch import LaunchWatcher

BEPINEX_START = (
    "[Message:   BepInEx] BepInEx 5.4.23.2 - valheim (11/21/2026 1:55:02 PM)\n"
    "[Info   :   BepInEx] Running under Unity v2022.3.50\n"
    "[Message:   BepInEx] Preloader started\n"
)


def test_waits_for_fresh_log_then_counts_mods(tmp_path):
    log = tmp_path / "LogOutput.log"
    log.write_text("[Message:   BepInEx] Chainloader startup complete\n")  # last session's log
    old = time.time() - 600
    os.utime(log, (old, old))
    w = LaunchWatcher(log, started_at=time.time())
    assert w.poll().phase == "waiting"  # stale file ignored

    log.write_text(BEPINEX_START)  # BepInEx truncates and starts writing
    assert w.poll().phase == "starting"

    with open(log, "a") as f:
        f.write("[Info   :   BepInEx] Loading [Jotunn 2.20.0]\n")
        f.write("[Info   :   BepInEx] Loading [LynxModFixes 1.4.2]\n")
        f.write("[Error  : Some Mod] something broke\n")
        f.write("[Info   :   BepInEx] Loading [Path of Valh")  # half-written line
    st = w.poll()
    assert st.phase == "loading" and st.loaded == 2 and st.last_mod == "LynxModFixes 1.4.2"
    assert len(st.errors) == 1

    with open(log, "a") as f:
        f.write("eiman 3.1.0]\n[Message:   BepInEx] Chainloader startup complete\n")
    st = w.poll()
    assert st.phase == "ready" and st.loaded == 3 and st.last_mod == "Path of Valheiman 3.1.0"


def test_missing_log_and_restart(tmp_path):
    log = tmp_path / "LogOutput.log"
    w = LaunchWatcher(log, started_at=time.time() - 5)
    assert w.poll().phase == "waiting"
    log.write_text(BEPINEX_START + "[Info   :   BepInEx] Loading [A 1.0]\n" * 5)
    assert w.poll().loaded == 5
    log.write_text(BEPINEX_START)  # game restarted: file truncated
    st = w.poll()
    assert st.loaded == 0 and st.phase == "starting"
