"""
Server Signals and Callbacks.

Inter-process communication mechanisms:
- Interrupt signals for task cancellation
- Exec-thread registry for execute_code async-exc termination
- Cycle-gap snippet executor scheduling
"""

from .interrupt import (
    request_interrupt,
    check_interrupt,
    clear_interrupt,
    set_current_task,
    clear_current_task,
    peek_current_task,
    register_interrupt_callback,
    ensure_cycle_callbacks,
    register_exec_thread,
    unregister_exec_thread,
    get_exec_thread,
)
from .cycle_executor import (
    submit_snippet,
    is_executor_callback_registered,
    register_executor_callback,
)

__all__ = [
    # Interrupt signals
    "request_interrupt",
    "check_interrupt",
    "clear_interrupt",
    "set_current_task",
    "clear_current_task",
    "peek_current_task",
    "register_interrupt_callback",
    "ensure_cycle_callbacks",
    # Exec-thread registry (execute_code async-exc termination)
    "register_exec_thread",
    "unregister_exec_thread",
    "get_exec_thread",
    # Cycle-gap executor
    "submit_snippet",
    "is_executor_callback_registered",
    "register_executor_callback",
]
