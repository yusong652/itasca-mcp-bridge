"""Tests for utils.command_splitter.preprocess_source.

The splitter breaks multi-line `itasca.command()` calls into one call
per command so the bridge's callback re-registration hook (which runs at
command-call boundaries) can repair the engine callback registry after
`model new`/`model restore` — see the command_splitter module docstring.
A blind spot in alias detection caused real-world scripts that use
`import itasca as it` to skip splitting entirely, which is how the
bridge wedged on first reproduction of the deadlock bug.

FISH definition blocks are the exception: they must survive splitting as
ONE multi-line command, because a lone `fish define <name>` drops the
console into interactive FISH mode and wedges the bridge.

These tests pin alias coverage and block preservation so the regressions
can't recur.
"""

from __future__ import annotations

import ast

from itasca_mcp_bridge.utils.command_splitter import preprocess_source


def _call_arg(call_line: str, call_name: str) -> str:
    """Extract the string-literal argument from a generated call line."""
    inner = call_line[len(call_name) + 1 : -1]
    return ast.literal_eval(inner)


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
    out = preprocess_source(src)
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
    out = preprocess_source(src)
    assert len(_call_lines(out, "it.command")) == 3


def test_bare_command_import_is_split():
    src = '''
from itasca import command

command("""
model new
model domain extent -1 1 -1 1 -1 1
""")
'''
    out = preprocess_source(src)
    assert len(_call_lines(out, "command")) == 2


def test_bare_command_aliased_import_is_split():
    src = '''
from itasca import command as cmd

cmd("""
model new
model domain extent -1 1 -1 1 -1 1
""")
'''
    out = preprocess_source(src)
    assert len(_call_lines(out, "cmd")) == 2


def test_single_command_is_not_split():
    src = '''
import itasca as it
it.command("model cycle 10000")
'''
    out = preprocess_source(src)
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
    out = preprocess_source(src)
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
    out = preprocess_source(src)
    # Source unchanged — the call shouldn't be split.
    assert out == src


def test_unrecognized_receiver_warns_in_log_and_output(caplog, capsys):
    # Reassignment pattern: `_it = itasca; _it.command("""...""")` —
    # AST can't prove _it is itasca, so the splitter passes it through.
    # The diagnostic must be agent-visible: WARNING in the bridge log
    # (root logger runs at INFO, so DEBUG would be filtered) AND printed
    # to stdout, which both execution paths redirect into the task/
    # snippet output before preprocessing.
    import logging

    src = '''
import itasca
_it = itasca
_it.command("""
model new
ball generate radius 0.1 number 10
""")
'''
    with caplog.at_level(logging.WARNING, logger="itasca-mcp-bridge"):
        out = preprocess_source(src)

    # Source unchanged (we don't try to split via reassignment).
    assert "_it.command(" in out
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("'_it'" in m and "splitter skipped" in m for m in warn_msgs), warn_msgs
    printed = capsys.readouterr().out
    assert "[bridge warning]" in printed and "'_it'" in printed


def test_fish_define_block_kept_whole():
    # The original wedge: line-splitting `fish define ... end` sent the
    # define alone and left the console waiting at the Fish> prompt.
    src = '''
import itasca as it

it.command("""
model new
fish define probe
    global probe_value
    probe_value = 314159
end
model cycle 100
""")
'''
    out = preprocess_source(src)
    ast.parse(out)  # generated source must be valid Python
    calls = _call_lines(out, "it.command")
    assert len(calls) == 3
    block = _call_arg(calls[1], "it.command")
    body = block.split("\n")
    assert body[0] == "fish define probe"
    assert body[-1] == "end"
    assert "probe_value = 314159" in block


def test_fish_operator_block_kept_whole():
    src = '''
import itasca

itasca.command("""
model new
fish operator par_op(a)
end
model cycle 10
""")
'''
    out = preprocess_source(src)
    calls = _call_lines(out, "itasca.command")
    assert len(calls) == 3
    block = _call_arg(calls[1], "itasca.command")
    assert block.split("\n")[0] == "fish operator par_op(a)"


def test_legacy_bare_define_block_kept_whole():
    # PFC 5-era scripts use bare `define ... end`; still accepted by
    # newer engines, so the block detection must cover it.
    src = '''
import itasca

itasca.command("""
model new
define legacy_fn
    legacy_fn = 1
End
""")
'''
    out = preprocess_source(src)
    calls = _call_lines(out, "itasca.command")
    assert len(calls) == 2
    block = _call_arg(calls[1], "itasca.command")
    assert block.split("\n")[0] == "define legacy_fn"
    # `End` terminator matched case-insensitively and kept verbatim
    assert block.split("\n")[-1] == "End"


def test_fish_block_only_call_is_left_unchanged():
    # A call containing ONLY one definition block has nothing to split;
    # the whole call passes through untouched (unsplit blocks work fine).
    src = '''
import itasca

itasca.command("""
fish define solo
    solo = 1
end
""")
'''
    out = preprocess_source(src)
    assert out == src


def test_unterminated_fish_block_passes_through_with_warning(caplog):
    import logging

    src = '''
import itasca

itasca.command("""
model new
fish define broken
    broken = 1
""")
'''
    with caplog.at_level(logging.WARNING, logger="itasca-mcp-bridge"):
        out = preprocess_source(src)

    calls = _call_lines(out, "itasca.command")
    assert len(calls) == 2
    block = _call_arg(calls[1], "itasca.command")
    assert block.split("\n")[0] == "fish define broken"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("without terminating 'end'" in m for m in warnings), warnings


def test_unrecognized_single_line_does_not_trigger_diagnostic(caplog, capsys):
    # Don't be noisy: single-line .command() on any receiver isn't a stall risk.
    import logging

    src = '''
other = something
other.command("just one line")
'''
    with caplog.at_level(logging.WARNING, logger="itasca-mcp-bridge"):
        preprocess_source(src)

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("splitter skipped" in m for m in warn_msgs), warn_msgs
    assert "[bridge warning]" not in capsys.readouterr().out
