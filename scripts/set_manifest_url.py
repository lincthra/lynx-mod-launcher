"""Bake the manifest URL into lynxlauncher/build_config.py (used by CI before PyInstaller)."""
import re
import sys
from pathlib import Path

url = sys.argv[1].strip() if len(sys.argv) > 1 else ""
if not url:
    print("no URL given; keeping the default in build_config.py")
    sys.exit(0)
if not re.match(r"^https?://\S+$", url):
    sys.exit(f"not a URL: {url!r}")
path = Path(__file__).resolve().parent.parent / "lynxlauncher" / "build_config.py"
text = path.read_text("utf-8")
new, n = re.subn(r'^MANIFEST_URL = .*$', f"MANIFEST_URL = {url!r}", text, flags=re.M)
if n != 1:
    sys.exit("MANIFEST_URL line not found")
path.write_text(new, "utf-8")
print(f"manifest URL set to {url}")
