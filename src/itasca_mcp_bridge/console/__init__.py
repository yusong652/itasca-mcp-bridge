# -*- coding: utf-8 -*-
"""User console capture: what the person types into the product GUI."""

from .history import ConsoleHistory, SOURCE_COMMAND, SOURCE_PYTHON
from .capture import install as install_console_capture, uninstall as uninstall_console_capture

__all__ = [
    "ConsoleHistory",
    "SOURCE_COMMAND",
    "SOURCE_PYTHON",
    "install_console_capture",
    "uninstall_console_capture",
]
