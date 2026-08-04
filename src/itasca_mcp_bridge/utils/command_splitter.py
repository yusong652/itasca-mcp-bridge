"""
Command Splitter - Preprocess scripts to split multi-line itasca.command() calls.

Why one command per call matters (verified live on PFC 6.00.030, 2026-08-04):

- The bridge's busy-time reachability (status polls, execute_code
  interleave, interrupt) comes entirely from the cycle callbacks
  registered via ``itasca.set_callback`` — the engine's C code never
  releases the GIL to other threads on its own.
- ``model new`` / ``model restore`` clear the engine's callback registry.
  The bridge re-registers them via a wrapper around ``itasca.command``
  (see ``signals.interrupt``), but that repair can only run AFTER a
  command call returns.
- A multi-line batch containing ``model new`` followed by ``model cycle``
  therefore wipes the registry mid-call and cycles with no callbacks at
  all: the bridge is unreachable and uninterruptible for the whole call.
  Splitting aligns command boundaries with the re-registration hook.
  Any engine whose model-reset commands clear the registry is affected,
  not just PFC 6.

FISH definition blocks (``fish define`` / ``fish operator`` / legacy bare
``define`` ... ``end``) are the exception and must be kept whole: sending
``fish define <name>`` on its own drops the console into interactive FISH
mode and the C call blocks holding the GIL waiting for the function body —
a total bridge wedge. Definition blocks execute instantly (the body runs
at call time, not define time), so keeping them as one multi-line command
is safe.

Python 3.6 compatible implementation.
"""

import ast
import logging
import re
from typing import List, Optional, Tuple

# Module logger
logger = logging.getLogger("itasca-mcp-bridge")


# Start of a FISH definition block: `fish define` / `fish operator`
# (abbreviations allowed, e.g. `fish def`) or the legacy bare `define`.
_FISH_BLOCK_START = re.compile(
    r"^(?:fish\s+(?:def\w*|oper\w*)|def\w*)\b", re.IGNORECASE
)
# End of a FISH definition block: a standalone `end` (optionally followed
# by a comment). Nested control blocks close with `endif`/`endloop`/
# `endsection`, so a bare `end` always terminates the definition.
_FISH_BLOCK_END = re.compile(r"^end\s*(?:;.*)?$", re.IGNORECASE)


def split_pfc_commands(multiline_str):
    # type: (str) -> List[str]
    """Split a multi-line ITASCA command string into individual commands.

    Handles:
    - Newline-separated commands
    - ITASCA line continuation with '...' at end of line
    - ITASCA comments starting with ';'
    - Empty/whitespace-only lines
    - FISH definition blocks (`fish define`/`fish operator`/legacy bare
      `define` ... `end`): kept whole as ONE multi-line command, body
      lines verbatim. Splitting a definition line-by-line would leave the
      console in interactive FISH mode with the C call blocked waiting
      for input (see module docstring). An unterminated block (no `end`)
      is emitted as-is — it is equally broken unsplit, and the engine's
      error message is clearer than anything we could synthesize.

    Args:
        multiline_str: Multi-line ITASCA command string

    Returns:
        List of individual ITASCA command strings
    """
    lines = multiline_str.split("\n")
    commands = []
    current = []  # type: List[str]
    block_lines = []  # type: List[str]
    in_block = False

    for line in lines:
        stripped = line.strip()

        if in_block:
            # Keep body lines verbatim (minus trailing whitespace): the
            # engine parses comments/continuations inside the block itself.
            block_lines.append(line.rstrip())
            if _FISH_BLOCK_END.match(stripped):
                commands.append("\n".join(block_lines))
                block_lines = []
                in_block = False
            continue

        # Skip empty lines and pure comment lines
        if not stripped or stripped.startswith(";"):
            continue

        if _FISH_BLOCK_START.match(stripped):
            in_block = True
            block_lines = [stripped]
            continue

        # Check for ITASCA line continuation (... at end)
        if stripped.endswith("..."):
            # Remove the '...' and accumulate
            current.append(stripped[:-3].rstrip())
            continue

        # No continuation — complete the current command
        current.append(stripped)
        joined = " ".join(current)
        if joined.strip():
            commands.append(joined.strip())
        current = []

    # Flush any remaining continuation
    if current:
        joined = " ".join(current)
        if joined.strip():
            commands.append(joined.strip())

    # Flush an unterminated FISH block (missing `end`): pass it through
    # unchanged so the engine reports the real error.
    if in_block and block_lines:
        logger.warning(
            "FISH definition block without terminating 'end' in "
            "multi-line command; passing through unsplit"
        )
        commands.append("\n".join(block_lines))

    return commands


def _get_string_value(node):
    # type: (ast.AST) -> Optional[str]
    """Extract string value from an AST node (Python 3.6+ compatible).

    Handles both ast.Str (Python 3.6-3.7) and ast.Constant (Python 3.8+).
    """
    # Python 3.6-3.7: ast.Str
    if hasattr(ast, "Str") and isinstance(node, ast.Str):
        return node.s
    # Python 3.8+: ast.Constant
    if hasattr(ast, "Constant") and isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
    return None


def _collect_itasca_aliases(tree):
    # type: (ast.Module) -> Tuple[set, set]
    """Collect names bound to itasca module / itasca.command in this script.

    Recognizes only top-level imports — local re-bindings inside functions
    are out of scope (the splitter only operates at module scope anyway).

    Returns:
        (module_aliases, bare_command_aliases)
        - module_aliases: names X where X.command(...) means itasca.command(...).
          Defaults to {"itasca"} so unqualified attribute calls still match
          when the user wrote a literal `import itasca`.
        - bare_command_aliases: names Y where Y(...) means itasca.command(...).
          Populated from `from itasca import command [as Y]`.
    """
    module_aliases = {"itasca"}
    bare_command_aliases = set()  # type: set

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "itasca":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "itasca":
                for alias in node.names:
                    if alias.name == "command":
                        bare_command_aliases.add(alias.asname or alias.name)

    return module_aliases, bare_command_aliases


def _is_command_call(node, module_aliases, bare_command_aliases):
    # type: (ast.Call, set, set) -> bool
    """Check if a Call node is itasca.command() (under any alias) or bare command()."""
    func = node.func

    # <alias>.command(...) — e.g. itasca.command(...), it.command(...)
    if isinstance(func, ast.Attribute):
        if (func.attr == "command"
                and isinstance(func.value, ast.Name)
                and func.value.id in module_aliases):
            return True

    # command(...) — from 'from itasca import command [as alias]'
    if isinstance(func, ast.Name) and func.id in bare_command_aliases:
        return True

    return False


def _find_multiline_command_calls(tree, module_aliases, bare_command_aliases):
    # type: (ast.Module, set, set) -> List[Tuple[ast.Call, str]]
    """Find all multi-line itasca.command() calls in the AST.

    Returns:
        List of (call_node, string_value) tuples, sorted by line number descending
        so replacements can be done back-to-front without shifting offsets.
    """
    results = []  # type: List[Tuple[ast.Call, str]]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_command_call(node, module_aliases, bare_command_aliases):
            continue
        # Must have exactly one positional string arg, no kwargs
        if len(node.args) != 1 or node.keywords:
            continue
        value = _get_string_value(node.args[0])
        if value is None:
            continue
        # Only process if it contains multiple ITASCA commands (newlines)
        if "\n" not in value:
            continue
        # Check it actually has >1 command after splitting
        commands = split_pfc_commands(value)
        if len(commands) <= 1:
            continue
        results.append((node, value))

    # Sort descending by line number (replace back-to-front)
    results.sort(key=lambda pair: pair[0].lineno, reverse=True)
    return results


def _find_unrecognized_multiline_command_calls(tree, module_aliases):
    # type: (ast.Module, set) -> List[Tuple[ast.Call, str]]
    """Find multi-line `*.command()` calls whose receiver isn't a known itasca alias.

    These slip through the splitter (we can't statically prove the receiver
    aliases itasca) and can wedge the bridge with the same deadlock the
    splitter prevents — typical cause is reassignment like `_it = itasca`
    or wrapping the alias inside a function.

    Returns (call_node, receiver_name) tuples for DEBUG-level logging.
    """
    findings = []  # type: List[Tuple[ast.Call, str]]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "command":
            continue
        if not isinstance(func.value, ast.Name):
            continue
        if func.value.id in module_aliases:
            continue  # would already be handled by the main splitter pass
        if len(node.args) != 1 or node.keywords:
            continue
        value = _get_string_value(node.args[0])
        if value is None or "\n" not in value:
            continue
        # Require >1 actual ITASCA command after splitting — single-line block
        # in triple-quotes isn't risky.
        if len(split_pfc_commands(value)) <= 1:
            continue
        findings.append((node, func.value.id))

    return findings


def _detect_call_name(call_node):
    # type: (ast.Call) -> str
    """Detect the command call name to use in replacement source.

    Reads the call name from the AST node itself rather than from the
    source text, so aliases like `import itasca as it` produce
    `it.command(...)` replacements (not `command(...)` which would
    NameError because the alias isn't bound that way).
    """
    func = call_node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return "{}.{}".format(func.value.id, func.attr)
    if isinstance(func, ast.Name):
        return func.id
    # Shouldn't reach here for nodes that passed _is_command_call,
    # but stay safe: fall back to the literal 'itasca.command'.
    return "itasca.command"


def _find_call_range(source_lines, call_node):
    # type: (List[str], ast.Call) -> Tuple[int, int]
    """Find the line range [start, end) of a call expression in source.

    Tracks parenthesis nesting to find the closing ')'.
    """
    start = call_node.lineno - 1  # 0-based
    depth = 0
    in_string = None  # type: Optional[str]
    escape_next = False

    for i in range(start, len(source_lines)):
        line = source_lines[i]
        for ch in line:
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if in_string:
                if ch == in_string:
                    in_string = None
                continue
            if ch in ('"', "'"):
                # Check for triple quotes
                rest = line[line.index(ch):]
                if rest.startswith('"""') or rest.startswith("'''"):
                    in_string = ch  # simplified: track single char
                else:
                    in_string = ch
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return (start, i + 1)  # end is exclusive

    # Fallback: couldn't find closing paren
    return (start, start + 1)


def _build_replacement(call_name, commands, indent):
    # type: (str, List[str], str) -> str
    """Build replacement source lines for split commands.

    Args:
        call_name: 'itasca.command' or 'command'
        commands: List of individual ITASCA command strings
        indent: Whitespace indentation to preserve

    Returns:
        Replacement source text with one call per line
    """
    lines = []
    for cmd in commands:
        # Escape backslashes, quotes, and newlines (FISH definition blocks
        # are emitted as one multi-line command) for the string literal.
        escaped = (
            cmd.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        )
        lines.append('{}{}("{}")'.format(indent, call_name, escaped))
    return "\n".join(lines)


def preprocess_source(source):
    # type: (str) -> str
    """Transform multi-line itasca.command() calls into individual calls.

    This is the main entry point, shared by the execute_task script path
    and the execute_code snippet path. If parsing or transformation
    fails, the original source is returned unchanged.

    Args:
        source: Raw Python source (script file content or snippet)

    Returns:
        Transformed source with split command calls
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Can't parse — return as-is (compile() will report the error later)
        return source

    module_aliases, bare_command_aliases = _collect_itasca_aliases(tree)

    # Diagnostic: multi-line .command() calls on receivers we couldn't
    # prove are itasca aliases (e.g. `_it = itasca; _it.command("""...""")`)
    # slip through splitting, so the bridge can be unreachable for the
    # whole call (and a mid-batch `model new` runs its remaining commands
    # with no cycle callbacks). Both execution paths redirect sys.stdout
    # before preprocessing, so the print lands in the task/snippet output
    # where the submitting agent actually reads.
    for node, receiver in _find_unrecognized_multiline_command_calls(tree, module_aliases):
        msg = (
            "[bridge warning] multi-line .command() on unrecognized receiver "
            "'{}' (line {}) — splitter skipped; if this aliases itasca, the "
            "bridge may be unreachable during this call. Call itasca.command "
            "via its import name (import itasca [as x] / from itasca import "
            "command) to get per-command splitting.".format(receiver, node.lineno)
        )
        logger.warning(msg)
        print(msg)

    calls = _find_multiline_command_calls(tree, module_aliases, bare_command_aliases)
    if not calls:
        return source

    source_lines = source.split("\n")

    for call_node, cmd_string in calls:
        # Detect indentation from source; call name from AST.
        start_line = source_lines[call_node.lineno - 1]
        indent = start_line[: len(start_line) - len(start_line.lstrip())]
        call_name = _detect_call_name(call_node)

        # Find full extent of the call in source
        line_start, line_end = _find_call_range(source_lines, call_node)

        # Split ITASCA commands and build replacement
        commands = split_pfc_commands(cmd_string)
        replacement = _build_replacement(call_name, commands, indent)

        # Replace the lines
        source_lines[line_start:line_end] = replacement.split("\n")

    result = "\n".join(source_lines)
    logger.debug("Preprocessed source: split %d multi-line command call(s)", len(calls))
    return result
