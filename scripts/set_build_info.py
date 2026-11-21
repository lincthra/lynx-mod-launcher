"""CI helper: stamp version (from the git tag) and the self-update URL into the package."""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
version, repo = (sys.argv[1:] + ["", ""])[:2]

if re.fullmatch(r"v?\d+\.\d+\.\d+([-+].*)?", version or ""):
    init = root / "lynxlauncher" / "__init__.py"
    text = re.sub(r'^__version__ = .*$', f'__version__ = "{version.lstrip("v")}"', init.read_text("utf-8"), flags=re.M)
    init.write_text(text, "utf-8")
    print(f"version {version.lstrip('v')}")
else:
    print("not a version tag; keeping __version__")

if repo:
    cfg = root / "lynxlauncher" / "build_config.py"
    url = f"https://github.com/{repo}/releases/latest/download/latest.json"
    text = re.sub(r'^UPDATE_URL = .*$', f"UPDATE_URL = {url!r}", cfg.read_text("utf-8"), flags=re.M)
    cfg.write_text(text, "utf-8")
    print(f"update URL {url}")
