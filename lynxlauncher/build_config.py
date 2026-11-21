# Baked into the launcher at build time. CI rewrites these lines with
# scripts/set_manifest_url.py (MANIFEST_URL repository variable) and sets UPDATE_URL to
# this repository's latest release. At run time, --manifest-url / LYNX_MANIFEST_URL and
# LYNX_UPDATE_URL override them (handy for testing).
MANIFEST_URL = "https://github.com/OWNER/valheim-modpack/releases/download/manifest/manifest.json"
# Where the launcher looks for newer versions of itself (empty = never self-update).
UPDATE_URL = ""
