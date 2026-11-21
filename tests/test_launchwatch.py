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


def test_chainloader_rewrites_log_longer_than_before(tmp_path):
    """The real bug: preloader writes the log, the chainloader truncates it and replays the
    preloader lines, so by the next poll the file is already longer than our last read."""
    log = tmp_path / "LogOutput.log"
    w = LaunchWatcher(log, started_at=time.time() - 5)
    preload = BEPINEX_START + "[Info   :HarmonyX] patching stuff\n" * 20
    log.write_text(preload)
    assert w.poll().phase == "starting"
    mods = "".join(f"[Info   :   BepInEx] Loading [Mod{i} 1.0.{i}]\n" for i in range(40))
    log.write_text(preload + "[Message:   BepInEx] Chainloader ready\n" + mods)  # rewritten, longer
    st = w.poll()
    assert st.loaded == 40 and st.last_mod == "Mod39 1.0.39" and st.phase == "loading"
    with open(log, "a") as f:
        f.write("[Message:   BepInEx] Chainloader startup complete\n")
    st = w.poll()
    assert st.phase == "ready" and st.loaded == 40


def test_fallback_when_info_not_logged_to_disk(tmp_path):
    log = tmp_path / "LogOutput.log"
    w = LaunchWatcher(log, started_at=time.time() - 5)
    log.write_text(
        BEPINEX_START
        + "[Message:   BepInEx] Chainloader started\n"
        + "[Warning:    Jotunn] something\n"
        + "[Message:LynxModFixes] hello\n"
        + "[Warning:    Jotunn] again\n"
    )
    st = w.poll()
    assert st.phase == "loading" and st.loaded == 0
    assert st.mod_sources == 2 and st.last_mod == "Jotunn"  # newest line wins


def test_reads_plugin_total(tmp_path):
    log = tmp_path / "LogOutput.log"
    w = LaunchWatcher(log, started_at=time.time() - 5)
    log.write_text(
        BEPINEX_START
        + "[Message:   BepInEx] Chainloader started\n"
        + "[Info   :   BepInEx] 87 plugins to load\n"
        + "[Info   :   BepInEx] Loading [Jotunn 2.20.0]\n"
    )
    st = w.poll()
    assert st.total == 87 and st.loaded == 1 and st.last_mod == "Jotunn 2.20.0"


REAL_5_4_23 = """[Message:   BepInEx] Chainloader ready
[Info   :LMF Startup] Bundle cache: on (10 AssetBundle load methods; cache in BepInEx/cache/LMF_bundles)
[Message:   BepInEx] Chainloader started
[Info   :   BepInEx] 61 plugins to load
[Info   :   BepInEx] Loading [SeneaL API Early 1.2.0] (__seneaL.valheim.api.early)
[Info   :   BepInEx] Loading [Conditional Config Sync 1.0.11] (_shudnal.ConditionalConfigSync)
[Info   :   BepInEx] Loading [Jotunn 2.30.2] (com.jotunn.jotunn)
[Info   :Jotunn.Main] Initializing ModCompatibility
[Info   :   BepInEx] Loading [Wire's Forge of Goodtential 0.6.1] (wire.valheim.forgeofgoodtential)
"""


def test_real_bepinex_5_4_23_lines_with_guid(tmp_path):
    log = tmp_path / "LogOutput.log"
    w = LaunchWatcher(log, started_at=time.time() - 5)
    log.write_text(BEPINEX_START + REAL_5_4_23)
    st = w.poll()
    assert st.total == 61 and st.loaded == 4
    assert st.last_mod == "Wire's Forge of Goodtential 0.6.1"
    with open(log, "a") as f:
        f.write("[Message:   BepInEx] Chainloader startup complete (61 loaded, 0 skipped, 0 failed)\n")
    st = w.poll()
    assert st.phase == "ready" and st.final == 61
