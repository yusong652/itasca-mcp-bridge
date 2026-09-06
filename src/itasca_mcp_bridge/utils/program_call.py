"""
Runtime expansion of ``program call`` into per-command engine calls.

Why: the engine holds the GIL for the whole duration of a C-level
``itasca.command``; the bridge is reachable during cycling only through
its own Python cycle callbacks (see ``utils.command_splitter`` and
``signals.interrupt``). ``program call '<file>'`` runs the entire data
file as ONE C call. A ``model new`` inside the file wipes the engine's
cycle-callback registry with no Python boundary to repair at, so every
``model cycle`` after it runs with no callbacks: the bridge is
unreachable and the task uninterruptible until the file returns.

Feeding the file's commands one at a time through the bridge's wrapped
``itasca.command`` gives every command the same guarantees as a command
written inline in the script: the callback-registry repair hook fires
after each ``model new``, cycling always runs with callbacks, and errors
carry the offending command.

Semantics reproduced from the engine (verified on PFC3D 9.7, 2026-09-06):

- Relative paths in a *nested* ``program call`` resolve against the
  directory of the calling file; a top-level call resolves against the
  process working directory.
- A missing extension is defaulted (``program call 'ret'`` finds
  ``ret.p3dat``).
- ``program return`` stops processing the current file.
- ``:label`` lines are ignored (comments) and are the targets of the
  ``label`` keyword; ``line <i>`` starts reading at line ``i``.
- ``suppress`` only affects echo; it is accepted and ignored here.

Anything this module cannot honor exactly is passed through to the
engine unchanged rather than approximated: multi-file calls (several
``call`` keywords on one line), unknown keywords, ``.py`` targets (the
engine runs those as Python), files it cannot resolve on disk (the
engine's own directory semantics and error message apply), files that
do not decode as text (``program encrypt`` output), and nesting deeper
than ``MAX_DEPTH``.

Python 3.6 compatible implementation.
"""

import logging
import os
import re
from typing import Any, Callable, List, Optional

from .command_splitter import split_engine_commands

logger = logging.getLogger("itasca-mcp-bridge")

# ``program call`` / ``prog call`` / bare ``call``. ``call`` may be
# abbreviated to ``cal`` at most; ``program c`` would be ambiguous with
# ``program continue`` and is left to the engine.
_CALL_RE = re.compile(r"^\s*(?:pro\w*\s+)?cal(?:l)?\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

# ``program return`` (any abbreviation of ``return``), optional comment.
_RETURN_RE = re.compile(r"^\s*(?:pro\w*\s+)?ret\w*\s*(?:;.*)?$", re.IGNORECASE)

# Tokens: single-quoted, double-quoted, or bare.
_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")

# Extensions tried, in order, when the target has none. Product data
# extensions first, then the generic ones.
DEFAULT_EXTENSIONS = (
    ".p3dat", ".p2dat",  # PFC
    ".f3dat", ".f2dat",  # FLAC3D / FLAC2D
    ".3ddat",            # 3DEC
    ".dat", ".fis",
)

# Nesting guard: deeper than this is passed to the engine untouched.
MAX_DEPTH = 16

# Directory stack of the files currently being expanded (innermost last),
# so nested relative paths resolve against the calling file's directory.
_dir_stack = []  # type: List[str]


class ParsedCall(object):
    """A ``program call`` this module can honor exactly."""

    __slots__ = ("target", "line", "label", "suppress")

    def __init__(self, target, line=None, label=None, suppress=False):
        # type: (str, Optional[int], Optional[str], bool) -> None
        self.target = target
        self.line = line
        self.label = label
        self.suppress = suppress


def _unquote(token):
    # type: (str) -> str
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def parse_program_call(cmd):
    # type: (str) -> Optional[ParsedCall]
    """Parse ``cmd`` as a ``program call`` the expander can honor.

    Returns None when ``cmd`` is not a ``program call`` or uses a form
    that must go to the engine unchanged (no target, several ``call``
    keywords, unknown keywords, malformed ``line``/``label``).
    """
    if "\n" in cmd:
        return None  # multi-line strings are the splitter's business
    m = _CALL_RE.match(cmd)
    if not m:
        return None
    rest = m.group(1)
    # Strip a trailing comment outside quotes.
    tokens = _TOKEN_RE.findall(rest)
    cut = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.startswith(";"):
            cut = i
            break
    tokens = tokens[:cut]
    if not tokens:
        return None
    target = _unquote(tokens[0])
    if not target:
        return None

    parsed = ParsedCall(target)
    i = 1
    while i < len(tokens):
        kw = tokens[i].lower()
        if kw.startswith("sup"):
            parsed.suppress = True
            i += 1
        elif kw.startswith("lin"):
            if i + 1 >= len(tokens):
                return None
            try:
                parsed.line = int(tokens[i + 1])
            except ValueError:
                return None
            i += 2
        elif kw.startswith("lab"):
            if i + 1 >= len(tokens):
                return None
            parsed.label = _unquote(tokens[i + 1])
            i += 2
        else:
            # ``call`` (multi-file) or anything unknown: engine's job.
            return None
    if parsed.line is not None and parsed.label is not None:
        return None
    return parsed


def resolve_target(target, base_dir):
    # type: (str, str) -> Optional[str]
    """Locate ``target`` on disk the way the engine would, or None."""
    path = target if os.path.isabs(target) else os.path.join(base_dir, target)
    if os.path.isfile(path):
        return path
    if not os.path.splitext(path)[1]:
        for ext in DEFAULT_EXTENSIONS:
            candidate = path + ext
            if os.path.isfile(candidate):
                return candidate
    return None


def _read_text(path):
    # type: (str) -> Optional[str]
    """Decode a data file; None if it is not text (encrypted/binary)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except (IOError, OSError):
        return None
    if b"\x00" in raw:
        return None
    for encoding in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _select_lines(lines, parsed):
    # type: (List[str], ParsedCall) -> Optional[List[str]]
    """Apply ``line``/``label`` and drop ``:label`` lines. None when the
    label is absent (the engine reports that error)."""
    if parsed.line is not None:
        lines = lines[max(parsed.line - 1, 0):]
    elif parsed.label is not None:
        want = parsed.label.lower()
        start = None
        for idx, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith(":") and stripped[1:].strip().lower() == want:
                start = idx + 1
                break
        if start is None:
            return None
        lines = lines[start:]
    return [raw for raw in lines if not raw.lstrip().startswith(":")]


def expand_program_call(cmd, run):
    # type: (str, Callable[[str], Any]) -> bool
    """Expand ``cmd`` if it is a ``program call`` this module can honor.

    ``run`` receives each command of the file in order; pass the bridge's
    wrapped ``itasca.command`` so the callback-registry repair hook and
    nested expansion apply per command.

    Returns True when the call was fully processed here, False when the
    caller must send ``cmd`` to the engine unchanged.
    """
    parsed = parse_program_call(cmd)
    if parsed is None:
        return False
    if len(_dir_stack) >= MAX_DEPTH:
        logger.warning("program call nesting deeper than %d; passing through", MAX_DEPTH)
        return False

    base_dir = _dir_stack[-1] if _dir_stack else os.getcwd()
    path = resolve_target(parsed.target, base_dir)
    if path is None:
        return False
    if path.lower().endswith(".py"):
        return False
    text = _read_text(path)
    if text is None:
        return False
    lines = _select_lines(text.splitlines(), parsed)
    if lines is None:
        return False

    commands = split_engine_commands("\n".join(lines))
    display = os.path.basename(path)
    print("[bridge] program call '{}': {} command(s) run inline".format(display, len(commands)))

    _dir_stack.append(os.path.dirname(os.path.abspath(path)))
    try:
        for command in commands:
            if _RETURN_RE.match(command):
                break
            try:
                run(command)
            except Exception as e:
                _annotate(e, display, command)
                raise
    finally:
        _dir_stack.pop()
    return True


def _annotate(exc, display, command):
    # type: (BaseException, str, str) -> None
    """Append the file and command to the engine's message, best-effort."""
    try:
        first = command.split("\n", 1)[0]
        note = "\n    While processing '{}' in file {}.".format(first, display)
        if exc.args and isinstance(exc.args[0], str):
            exc.args = (exc.args[0] + note,) + tuple(exc.args[1:])
    except Exception:
        pass
