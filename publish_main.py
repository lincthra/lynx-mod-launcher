"""PyInstaller entry point for the publisher (optional; it also runs fine from source)."""
import sys

from lynxlauncher.publisher import main

if __name__ == "__main__":
    sys.exit(main())
