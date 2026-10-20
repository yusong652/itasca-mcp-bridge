"""
Main Thread Executor - Thread-safe task queue for main thread execution.

This module provides task queue mechanism to execute ITASCA commands in the main thread
while the HTTP server runs in background thread.

Python 3.6 compatible implementation.
"""

import queue
import logging
import threading
from concurrent.futures import Future
from typing import Callable, Any, List, Optional

from ..utils import modal_guard

# Module logger
logger = logging.getLogger("itasca-mcp-bridge")


class EngineAbandonedCall(Exception):
    """The engine unwound a call past Python and never returned to it."""


class _Call(object):
    """A call started on the main thread and not yet seen finish."""

    __slots__ = ("func", "future", "guard_depth", "dialog_mark", "unwind", "on_abandon")

    def __init__(self, func, future, unwind):
        self.func = func
        self.future = future
        self.guard_depth = modal_guard.depth()
        self.dialog_mark = modal_guard.dialog_mark()
        self.unwind = unwind  # type: Optional[Callable[[], None]]
        self.on_abandon = None  # type: Optional[Callable[[str], Any]]


# Calls in progress on the main thread, outermost first: the executor's,
# and the snippets a cycle callback runs inside them.
#
# Some engine errors never become Python exceptions. Measured on PFC3D 7.00
# GUI with ``itasca.facetarray.set_conveyor`` given a velocity normal to the
# facets: the engine's C++ error unwinds straight through the interpreter,
# shows in a message box, and the engine carries on from wherever it caught
# it -- the product's Qt event loop for a call the pump made, the cycle loop
# (which then fails the cycling command with the same message) for a call
# made from a cycle callback. The Python frames in between are never
# resumed, ``finally`` blocks included, so without help the request waits
# out its timeout and everything the call had set up stays set up: the
# bridge's stdout tee, its engine-console capture, its cycle-callback depth,
# and the request's interrupt flag, which then stops every later cycle
# typed in the GUI console.
#
# Such a call is noticed when the code that made it runs again. The pump's
# timer is never delivered while its own handler is on the stack (Qt will
# not recurse into a timer), and the blocking pump calls process_tasks in a
# plain loop, so a tick that finds a call still listed here proves that
# call's frames are gone; so does an engine command that returns while a
# call started inside it is still listed.
_running = []  # type: List[_Call]


def begin_call(func, future, unwind=None):
    # type: (Callable[..., Any], Future, Optional[Callable[[], None]]) -> _Call
    """List a call about to run on the main thread, and open the guard's
    window for it: the engine can raise a modal error box anywhere in the
    call, not only inside itasca.command.

    ``unwind`` undoes what the caller set up around the call, for when the
    engine abandons it.
    """
    call = _Call(func, future, unwind)
    _running.append(call)
    modal_guard.entered()
    return call


def end_call(call):
    # type: (_Call) -> None
    """The call returned. One already settled as abandoned is left alone."""
    if _running and _running[-1] is call:
        _running.pop()
        modal_guard.left()


def running_depth():
    # type: () -> int
    """How many calls are listed, to hand to ``settle_abandoned`` later."""
    return len(_running)


def on_abandon(func, handler):
    # type: (Callable[..., Any], Callable[[str], Any]) -> None
    """Have ``handler(message)`` settle the current call to ``func`` if the
    engine abandons it; its return value becomes the call's result.

    The handler runs in place of the cleanup the call can no longer do. It
    is registered only when ``func`` is the innermost call listed, so
    ``func`` reached some other way cannot take over another call's handler.
    """
    if _running and _running[-1].func == func and _running[-1].on_abandon is None:
        _running[-1].on_abandon = handler


def _abandoned_message(call, error):
    # type: (_Call, Optional[BaseException]) -> str
    # What the engine said: in its message box when it unwound to the event
    # loop, in the exception it raised from the cycling command otherwise.
    said = modal_guard.dialogs_since(call.dialog_mark)
    if not said and error is not None:
        said = [str(error).strip().split("\n", 1)[0]]
    message = (
        "The engine abandoned this call without returning to Python, so the "
        "code after that point did not run."
    )
    if said:
        message = "Engine error: {} {}".format(" | ".join(said), message)
    return message


def settle_abandoned(depth=0, error=None):
    # type: (int, Optional[BaseException]) -> int
    """Settle the calls listed beyond ``depth``, innermost first: the code
    that started them is running again, so they will never return.

    ``error`` is what the engine command they ran inside raised, if any.
    """
    settled = 0
    while len(_running) > depth:
        call = _running.pop()
        settled += 1
        # Also closes any engine command the call was inside when abandoned.
        modal_guard.unwind_to(call.guard_depth)
        message = _abandoned_message(call, error)
        logger.error(
            "Call abandoned by the engine: %s - %s",
            getattr(call.func, "__name__", call.func), message
        )
        if call.unwind is not None:
            try:
                call.unwind()
            except Exception as e:
                logger.error("Unwinding an abandoned call failed: %s", e)
        if call.future.done():
            continue
        if call.on_abandon is None:
            call.future.set_exception(EngineAbandonedCall(message))
            continue
        try:
            call.future.set_result(call.on_abandon(message))
        except Exception as e:
            call.future.set_exception(e)
    return settled


class MainThreadExecutor:
    """
    Execute tasks in ITASCA IPython main thread via queue.

    HTTP server (background thread) submits tasks via submit(),
    IPython main thread processes tasks via process_tasks().
    """

    def __init__(self):
        """Initialize executor with thread-safe queue."""
        self.task_queue = queue.Queue()
        self.main_thread_id = threading.current_thread().ident
        self.main_thread_name = threading.current_thread().name
        logger.info(
            "MainThreadExecutor initialized (main_thread=%s, id=%s)",
            self.main_thread_name, self.main_thread_id
        )

    def submit(self, func, *args, **kwargs):
        # type: (Callable[..., Any], Any, Any) -> Future
        """
        Submit task to main thread queue (called from background thread).

        Args:
            func: Function to execute in main thread
            *args: Positional arguments for function
            **kwargs: Keyword arguments for function

        Returns:
            Future: Future object to block on for the result

        Example:
            # From a request (HTTP handler) thread
            future = executor.submit(itasca.command, "ball generate number 100")
            result = future.result(timeout)
        """
        future = Future()
        self.task_queue.put((func, args, kwargs, future))
        logger.debug(
            "Task submitted: %s (queue_size=%d)",
            func.__name__, self.task_queue.qsize()
        )
        return future

    def process_tasks(self, max_tasks: Optional[int] = None) -> int:
        """
        Process all pending tasks in queue (called from main thread).

        This method should be called from IPython main thread, either:
        - Via post_execute hook (automatic after each IPython command)
        - Via manual loop (run_task_loop())

        Args:
            max_tasks: Optional maximum number of tasks to process in this call.
                - None (default): process all pending tasks
                - int > 0: process up to max_tasks then return

        Returns:
            int: Number of tasks processed

        Note:
            Non-blocking - processes all available tasks and returns.
        """
        task_limit = None  # type: Optional[int]
        if max_tasks is not None:
            try:
                parsed = int(max_tasks)
            except (TypeError, ValueError):
                parsed = 1
            if parsed > 0:
                task_limit = parsed

        # Check if we're in the main thread
        current_thread_id = threading.current_thread().ident
        current_thread_name = threading.current_thread().name
        is_main_thread = (current_thread_id == self.main_thread_id)

        if not is_main_thread:
            logger.warning(
                "process_tasks() called from wrong thread: "
                "current=%s (id=%s), expected=%s (id=%s)",
                current_thread_name, current_thread_id,
                self.main_thread_name, self.main_thread_id
            )

        # Nothing can still be running when the pump ticks; see _running.
        if _running:
            settle_abandoned()

        processed_count = 0

        # Process all pending tasks
        while True:
            if task_limit is not None and processed_count >= task_limit:
                break
            try:
                # Non-blocking get
                func, args, kwargs, future = self.task_queue.get_nowait()
                processed_count += 1

                # Log thread information for first task
                if processed_count == 1:
                    thread_status = "main_thread" if is_main_thread else "wrong_thread"
                    logger.debug(
                        "Processing tasks (thread=%s, name=%s, id=%s)",
                        thread_status, current_thread_name, current_thread_id
                    )

                # Mark future as running (enables future.running() check)
                # Returns False if future was cancelled - skip execution
                if not future.set_running_or_notify_cancel():
                    logger.debug("Task skipped (cancelled): %s", func.__name__)
                    continue

                call = begin_call(func, future)
                try:
                    # Execute task
                    result = func(*args, **kwargs)
                    if not future.done():
                        future.set_result(result)
                    logger.debug("Task completed: %s", func.__name__)

                except Exception as e:
                    # Set exception on future
                    if not future.done():
                        future.set_exception(e)
                    logger.error("Task failed: %s - %s", func.__name__, e)

                finally:
                    end_call(call)

            except queue.Empty:
                # Queue empty, exit
                break

        if processed_count > 0:
            logger.debug("Processed %d task(s)", processed_count)

        return processed_count

    def queue_size(self):
        # type: () -> int
        """
        Get current queue size.

        Returns:
            int: Number of pending tasks
        """
        return self.task_queue.qsize()
