# The manifest URL baked into the launcher.
# CI rewrites this line with scripts/set_manifest_url.py before building, using the
# MANIFEST_URL repository variable. At run time, --manifest-url or the
# LYNX_MANIFEST_URL environment variable override it (handy for testing).
# With GitHub storage it is https://github.com/OWNER/REPO/releases/download/manifest/manifest.json
MANIFEST_URL = "https://github.com/OWNER/valheim-modpack/releases/download/manifest/manifest.json"
