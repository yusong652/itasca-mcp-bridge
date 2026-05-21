"""Tests for utils.command_splitter.preprocess_script.

The splitter exists to break multi-line `itasca.command()` calls into
one call per command so the PFC C extension releases the GIL between
sub-commands. A blind spot in alias detection caused real-world scripts
that use `import itasca as it` to skip splitting entirely, which is
how the bridge wedged on first reproduction of the deadlock bug.

These tests pin alias coverage so the regression can't recur.
"""

from __future__ import annotations

from itasca_mcp_bridge.utils.command_splitter import preprocess_script


def _call_lines(src: str, call_name: str) -> list[str]:
    """Extract output lines that begin with `<call_name>(`."""
    needle = call_name + "("
    return [line.strip() for line in src.split("\n") if line.strip().startswith(needle)]


def test_itasca_command_canonical_form_is_split():
    src = '''
import itasca

itasca.command("""
model new
model domain extent -1 1 -1 1 -1 1
""")
'''
    out = preprocess_script(src)
    assert len(_call_lines(out, "itasca.command")) == 2


def test_aliased_it_command_is_split():
    # This is the regression case: `import itasca as it` was previously
    # not recognized, so the multi-line block ran as a single C batch.
    src = '''
import itasca as it

it.command("""
model new
model domain extent -1 1 -1 1 -1 1
ball generate radius 0.1 number 10
""")
'''
    out = preprocess_script(src)
    assert len(_call_lines(out, "it.command")) == 3


def test_bare_command_import_is_split():
    src = '''
from itasca import command

command("""
model new
model domain extent -1 1 -1 1 -1 1
""")
'''
    out = preprocess_script(src)
    assert len(_call_lines(out, "command")) == 2


def test_bare_command_aliased_import_is_split():
    src = '''
from itasca import command as cmd

cmd("""
model new
model domain extent -1 1 -1 1 -1 1
""")
'''
    out = preprocess_script(src)
    assert len(_call_lines(out, "cmd")) == 2


def test_single_command_is_not_split():
    src = '''
import itasca as it
it.command("model cycle 10000")
'''
    out = preprocess_script(src)
    # Source unchanged
    assert out == src


def test_no_import_defaults_to_itasca_alias():
    # Defensive baseline: even without an explicit `import itasca`,
    # `itasca.command(...)` should still split (the original behavior).
    src = '''
itasca.command("""
model new
ball generate radius 0.1 number 10
""")
'''
    out = preprocess_script(src)
    assert len(_call_lines(out, "itasca.command")) == 2


def test_unrelated_command_attr_is_not_touched():
    # `something.command(...)` where `something` is not bound to itasca
    # must NOT be treated as a PFC command.
    src = '''
import some_other_module as other
other.command("""
multiline content
that is not pfc
""")
'''
    out = preprocess_script(src)
    # Source unchanged — the call shouldn't be split.
    assert out == src


def test_unrecognized_receiver_emits_debug_diagnostic(caplog):
    # Reassignment pattern: `_it = itasca; _it.command("""...""")` —
    # AST can't prove _it is itasca, so the splitter passes it through.
    # We should at least leave a DEBUG breadcrumb so future "bridge stalled"
    # reports have something to grep for.
    import logging

    src = '''
import itasca
_it = itasca
_it.command("""
model new
ball generate radius 0.1 number 10
""")
'''
    with caplog.at_level(logging.DEBUG, logger="itasca-mcp-bridge"):
        out = preprocess_script(src)

    # Source unchanged (we don't try to split via reassignment).
    assert "_it.command(" in out
    # But a diagnostic was logged.
    debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("'_it'" in m and "splitter skipped" in m for m in debug_msgs), debug_msgs


def test_unrecognized_single_line_does_not_trigger_diagnostic(caplog):
    # Don't be noisy: single-line .command() on any receiver isn't a stall risk.
    import logging

    src = '''
other = something
other.command("just one line")
'''
    with caplog.at_level(logging.DEBUG, logger="itasca-mcp-bridge"):
        preprocess_script(src)

    debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert not any("splitter skipped" in m for m in debug_msgs), debug_msgs
