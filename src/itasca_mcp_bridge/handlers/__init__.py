"""
HTTP bridge message handlers.

This module provides modular message handlers for the ITASCA HTTP bridge.
Each handler module focuses on a specific domain of functionality. Handlers
are plain synchronous callables ``(ctx, data) -> dict``; the transport
(server.py) serves each one on its own request thread.
"""

from .context import ServerContext
from .tasks import (
    handle_execute_task,
    handle_check_task_status,
    handle_list_tasks,
    handle_interrupt_task,
)
from .execute_code import handle_execute_code

__all__ = [
    # Context
    "ServerContext",
    # Tasks
    "handle_execute_task",
    "handle_check_task_status",
    "handle_list_tasks",
    "handle_interrupt_task",
    # Execute code
    "handle_execute_code",
]
