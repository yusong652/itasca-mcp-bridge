# -*- coding: utf-8 -*-
"""User console capture: what the person types into the product GUI, and
which plots and data files they have open."""

from .history import (
    ConsoleHistory,
    SOURCE_COMMAND,
    SOURCE_PLOT_ITEM,
    SOURCE_PYTHON,
    SOURCE_VIEW,
)
from .capture import install as install_console_capture, uninstall as uninstall_console_capture
from .views import install as install_view_tracking, uninstall as uninstall_view_tracking

__all__ = [
    "ConsoleHistory",
    "SOURCE_COMMAND",
    "SOURCE_PLOT_ITEM",
    "SOURCE_PYTHON",
    "SOURCE_VIEW",
    "install_console_capture",
    "install_view_tracking",
    "uninstall_console_capture",
    "uninstall_view_tracking",
]
