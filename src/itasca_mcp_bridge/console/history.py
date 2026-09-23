# -*- coding: utf-8 -*-
"""Console history: what the person at the keyboard typed, kept for the client.

Entries come from two places in the product GUI -- the IPython pane and the
native command line -- and are appended by the main thread as they happen.
The MCP client is stateless: it calls ``consume()`` and gets whatever it has
not seen yet. The delivery cursor lives here, on disk next to the entries,
so a client that reconnects does not replay the whole session and does not
lose the entries it missed while it was away.

Persistence is one JSON object per line, appended as entries arrive.
Crash-safe in the only way that matters: a torn write costs the last line,
never the file.

Python 3.6 compatible implementation.
"""

import json
import logging
import os
import threading
import time

from ..utils import path_utils

logger = logging.getLogger("itasca-mcp-bridge")

HISTORY_FILENAME = "console_history.jsonl"
CURSOR_FILENAME = "console_cursor.json"

DEFAULT_MAX_ENTRIES = 500

# Where an entry came from. "python" is a cell run in the IPython pane;
# "command" is a line entered at the product's own command prompt.
SOURCE_PYTHON = "python"
SOURCE_COMMAND = "command"


class ConsoleHistory:
    """Append-only history of user console input with a delivery cursor.

    Appends happen on the product's main thread (Qt slots, IPython hooks);
    ``consume`` runs on an HTTP request thread. One lock covers both.
    """

    def __init__(self, max_entries=DEFAULT_MAX_ENTRIES, directory=None):
        # type: (int, str) -> None
        self._max_entries = max_entries
        directory = directory or path_utils.data_dir()
        self._path = os.path.join(directory, HISTORY_FILENAME)
        self._cursor_path = os.path.join(directory, CURSOR_FILENAME)
        self._lock = threading.Lock()
        self._entries = []  # type: list
        self._next_id = 1
        self._last_delivered_id = 0
        # Called with the new entry after each add; the server hangs its
        # SSE doorbell here. Never allowed to break an add.
        self.on_new_entry = None

        if not os.path.exists(directory):
            os.makedirs(directory)

        self._load()
        self._load_cursor()
        logger.info(
            "ConsoleHistory ready (%d entries, cursor=%d)",
            len(self._entries), self._last_delivered_id,
        )

    # -- writing -------------------------------------------------------------

    def add(self, source, input_text, output="", result=None, success=True):
        # type: (str, str, str, object, bool) -> dict
        """Record one entry and return it.

        ``source`` is ``"python"`` or ``"command"``; ``input_text`` is what
        was typed; ``output`` is what it printed; ``result`` is the value of
        a Python expression cell, if any; ``success`` is False when the cell
        raised. Command-line entries carry no result and report success
        unless the engine's error marker was seen in their output.
        """
        entry = {
            "id": None,
            "source": source,
            "input": input_text,
            "output": output,
            "result": _serialize(result),
            "success": bool(success),
            "timestamp": time.time(),
        }
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._entries.append(entry)
            self._append_to_file(entry)
            self._prune()

        callback = self.on_new_entry
        if callback is not None:
            try:
                callback(entry)
            except Exception as e:
                logger.error("Console history callback failed: %s", e)
        return entry

    # -- reading -------------------------------------------------------------

    def consume(self, limit=20):
        # type: (int) -> dict
        """Return the entries not yet delivered and move the cursor past them.

        At most ``limit`` entries come back -- the newest ones, so a client
        that was away for a long session sees what just happened rather
        than what happened first. ``has_more`` is False after a consume
        by construction (the cursor moves to the newest returned entry);
        it is reported so the client need not infer it.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        if limit <= 0:
            limit = 20

        with self._lock:
            entries = [e for e in self._entries if e["id"] > self._last_delivered_id]
            if len(entries) > limit:
                entries = entries[-limit:]
            if entries:
                self._last_delivered_id = entries[-1]["id"]
                self._save_cursor()
            has_more = any(e["id"] > self._last_delivered_id for e in self._entries)
            return {
                "entries": [dict(e) for e in entries],
                "cursor": self._last_delivered_id,
                "has_more": has_more,
            }

    def __len__(self):
        with self._lock:
            return len(self._entries)

    # -- persistence ---------------------------------------------------------

    def _append_to_file(self, entry):
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("Failed to write console history: %s", e)

    def _load(self):
        if not os.path.exists(self._path):
            return
        entries = []
        max_id = 0
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue  # torn or foreign line
                    if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
                        continue
                    entries.append(entry)
                    if entry["id"] > max_id:
                        max_id = entry["id"]
        except OSError as e:
            logger.warning("Failed to load console history: %s", e)
            return
        if len(entries) > self._max_entries:
            entries = entries[-self._max_entries:]
        self._entries = entries
        self._next_id = max_id + 1

    def _load_cursor(self):
        if not os.path.exists(self._cursor_path):
            return
        try:
            with open(self._cursor_path, encoding="utf-8") as f:
                data = json.load(f)
            value = data.get("last_delivered_id", 0)
            if isinstance(value, int):
                self._last_delivered_id = value
        except (OSError, ValueError, AttributeError):
            pass

    def _save_cursor(self):
        try:
            with open(self._cursor_path, "w", encoding="utf-8") as f:
                json.dump({"last_delivered_id": self._last_delivered_id}, f)
        except OSError as e:
            logger.error("Failed to save console cursor: %s", e)

    def _prune(self):
        if len(self._entries) <= self._max_entries:
            return
        self._entries = self._entries[-self._max_entries:]
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                for entry in self._entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("Failed to rewrite console history: %s", e)


def _serialize(value):
    """JSON-safe form of an expression result: scalars as-is, the rest as repr."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable {}>".format(type(value).__name__)
