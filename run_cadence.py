"""Frozen-build entry point (PyInstaller needs a real script, not -m)."""

import multiprocessing
import sys

from cadence.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
