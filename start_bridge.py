# -*- coding: utf-8 -*-
"""
Itasca Bridge source-run entrypoint.

Run the bridge directly from a source checkout (no PyPI install),
from the product's IPython console:
    %run /path/to/itasca-bridge/start_bridge.py

Equivalent to:
    import itasca_bridge
    itasca_bridge.start()
"""

import os
import sys


def _ensure_local_src_on_path():
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)


_ensure_local_src_on_path()

import itasca_bridge  # type: ignore  # noqa: E402


def main():
    itasca_bridge.start()


if __name__ == "__main__":
    main()
