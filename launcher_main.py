"""PyInstaller entry point for the friend-facing launcher."""
import sys

from lynxlauncher.launcher_app import main

if __name__ == "__main__":
    sys.exit(main())
