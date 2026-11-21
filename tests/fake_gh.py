"""A stand-in for the GitHub CLI: keeps releases/assets in a folder laid out like
github.com/<repo>/releases/download/<tag>/<asset>, so a local HTTP server can serve it."""
import json
import os
import shutil
import sys
from pathlib import Path

root = Path(os.environ["FAKE_GH_ROOT"])
args = sys.argv[1:]
repo = args[args.index("--repo") + 1]
args = args[: args.index("--repo")] + args[args.index("--repo") + 2:]
base = root / repo / "releases" / "download"
log = root / "calls.log"
with log.open("a") as f:
    f.write(" ".join(args[:3]) + "\n")

if args[:2] == ["release", "list"]:
    tags = sorted(p.name for p in base.iterdir()) if base.is_dir() else []
    print(json.dumps([{"tagName": t} for t in tags]))
elif args[:2] == ["release", "create"]:
    if os.environ.get("FAKE_GH_EMPTY_REPO"):
        print("HTTP 422: Repository is empty.", file=sys.stderr)
        sys.exit(1)
    (base / args[2]).mkdir(parents=True, exist_ok=True)
elif args[:2] == ["release", "view"]:
    d = base / args[2]
    print(json.dumps({"assets": [{"name": p.name} for p in d.iterdir()]}))
elif args[:2] == ["release", "upload"]:
    tag = args[2]
    clobber = "--clobber" in args
    for f in (a for a in args[3:] if a != "--clobber"):
        dst = base / tag / Path(f).name
        if Path(f).stat().st_size == 0:
            print("HTTP 400: Bad Content-Length", file=sys.stderr)
            sys.exit(1)
        if dst.exists() and not clobber:
            print(f"asset {dst.name} already exists", file=sys.stderr)
            sys.exit(1)
        shutil.copyfile(f, dst)
elif args[:2] == ["release", "delete-asset"]:
    (base / args[2] / args[3]).unlink()
else:
    print(f"fake gh: unsupported {args}", file=sys.stderr)
    sys.exit(2)
