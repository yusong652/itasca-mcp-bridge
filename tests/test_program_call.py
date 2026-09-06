"""Tests for utils.program_call — runtime expansion of `program call`.

The engine runs a called data file as one C call, so a `model new`
inside it wipes the cycle-callback registry with no Python boundary to
repair at, and every later `model cycle` wedges the bridge. Expansion
feeds the file's commands through the wrapped itasca.command one at a
time. Engine semantics reproduced here were verified live on PFC3D 9.7.
"""

from __future__ import annotations

import os

import pytest
from itasca_mcp_bridge.utils import program_call as pc
from itasca_mcp_bridge.utils.program_call import (
    expand_program_call,
    parse_program_call,
    resolve_target,
)


@pytest.fixture(autouse=True)
def _cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert pc._dir_stacks == {}
    yield
    assert pc._dir_stacks == {}, "directory stacks must unwind and be released"


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestParse:
    @pytest.mark.parametrize(
        "cmd",
        [
            "program call 'run.p3dat'",
            'program call "run.p3dat"',
            "prog call 'run.p3dat'",
            "call 'run.p3dat'",
            "  PROGRAM CALL run.p3dat  ; trailing comment",
        ],
    )
    def test_forms(self, cmd):
        parsed = parse_program_call(cmd)
        assert parsed is not None
        assert parsed.target == "run.p3dat"

    def test_keywords(self):
        p = parse_program_call("program call 'a.dat' suppress line 5")
        assert p is not None and p.suppress and p.line == 5 and p.label is None
        p = parse_program_call("program call 'a.dat' label 'start'")
        assert p is not None and p.label == "start"

    @pytest.mark.parametrize(
        "cmd",
        [
            "model cycle 100",
            "program continue",
            "program c",  # ambiguous abbreviation: engine decides
            "program call",  # no target
            "program call 'a.dat' call 'b.dat'",  # multi-file: engine order semantics
            "program call 'a.dat' bogus",  # unknown keyword
            "program call 'a.dat' line x",  # malformed
            "program call 'a.dat' line 2 label x",  # mutually exclusive
            "program call 'a.dat'\nmodel cycle 1",  # multi-line: splitter's job
            "calculate something",
        ],
    )
    def test_passthrough_forms(self, cmd):
        assert parse_program_call(cmd) is None


class TestResolve:
    def test_exact_and_default_extension(self, tmp_path):
        f = _write(tmp_path / "ret.p3dat", "model cycle 1\n")
        assert resolve_target("ret.p3dat", str(tmp_path)) == str(f)
        assert resolve_target("ret", str(tmp_path)) == str(f)
        assert resolve_target("missing", str(tmp_path)) is None

    def test_absolute_path_ignores_base(self, tmp_path):
        f = _write(tmp_path / "abs.dat", "model cycle 1\n")
        assert resolve_target(str(f), "/nowhere") == str(f)


class TestExpand:
    def test_not_a_call_is_untouched(self):
        ran = []
        assert expand_program_call("model cycle 100", ran.append) is False
        assert ran == []

    def test_feeds_commands_one_at_a_time(self, tmp_path):
        _write(
            tmp_path / "run.p3dat",
            "; header comment\nmodel new\nmodel domain extent -10 10 ...\n   condition destroy\nmodel cycle 100\n",
        )
        ran = []
        assert expand_program_call("program call 'run.p3dat'", ran.append) is True
        assert ran == [
            "model new",
            "model domain extent -10 10 condition destroy",
            "model cycle 100",
        ]

    def test_fish_block_kept_whole(self, tmp_path):
        _write(
            tmp_path / "f.p3dat",
            "fish define hello\n  io.out('hi')\nend\n@hello\n",
        )
        ran = []
        expand_program_call("call 'f.p3dat'", ran.append)
        assert ran == ["fish define hello\n  io.out('hi')\nend", "@hello"]

    def test_program_return_stops_file(self, tmp_path):
        _write(tmp_path / "ret.p3dat", "ball create id 1\nprogram return\nball create id 2\n")
        ran = []
        expand_program_call("program call 'ret'", ran.append)
        assert ran == ["ball create id 1"]

    def test_nested_relative_resolves_against_calling_file_dir(self, tmp_path):
        # Verified engine behaviour: `program call 'inner'` inside
        # sub/outer.p3dat finds sub/inner.p3dat, not ./inner.p3dat.
        _write(tmp_path / "sub" / "outer.p3dat", "ball create id 1\nprogram call 'inner.p3dat'\nball create id 3\n")
        _write(tmp_path / "sub" / "inner.p3dat", "ball create id 2\n")
        _write(tmp_path / "inner.p3dat", "ball create id 999\n")
        ran = []

        def run(cmd):
            # Mirror the wrapped itasca.command: nested calls come back here.
            if not expand_program_call(cmd, run):
                ran.append(cmd)

        assert expand_program_call("program call 'sub/outer.p3dat'", run) is True
        assert ran == ["ball create id 1", "ball create id 2", "ball create id 3"]

    def test_line_and_label_keywords(self, tmp_path):
        _write(tmp_path / "l.p3dat", "ball create id 1\n:start\nball create id 2\n:other\nball create id 3\n")
        ran = []
        expand_program_call("program call 'l.p3dat' label start", ran.append)
        assert ran == ["ball create id 2", "ball create id 3"]  # :other dropped as comment
        ran = []
        expand_program_call("program call 'l.p3dat' line 3", ran.append)
        assert ran == ["ball create id 2", "ball create id 3"]

    def test_missing_label_passes_through(self, tmp_path):
        _write(tmp_path / "l.p3dat", "ball create id 1\n")
        ran = []
        assert expand_program_call("program call 'l.p3dat' label nope", ran.append) is False
        assert ran == []

    def test_unresolvable_file_passes_through(self):
        ran = []
        assert expand_program_call("program call 'nope.p3dat'", ran.append) is False
        assert ran == []

    def test_python_target_passes_through(self, tmp_path):
        _write(tmp_path / "s.py", "print(1)\n")
        ran = []
        assert expand_program_call("program call 's.py'", ran.append) is False

    def test_binary_file_passes_through(self, tmp_path):
        (tmp_path / "enc.p3dat").write_bytes(b"\x00\x01garbage\x00")
        ran = []
        assert expand_program_call("program call 'enc.p3dat'", ran.append) is False

    def test_gbk_comment_does_not_break_decoding(self, tmp_path):
        (tmp_path / "cn.p3dat").write_bytes("; 注释\nmodel cycle 1\n".encode("gbk"))
        ran = []
        assert expand_program_call("program call 'cn.p3dat'", ran.append) is True
        assert ran == ["model cycle 1"]

    def test_error_is_annotated_and_stack_unwinds(self, tmp_path):
        _write(tmp_path / "bad.p3dat", "ball create id 1\nbogus command\nball create id 2\n")

        def run(cmd):
            if cmd.startswith("bogus"):
                raise ValueError("Unknown command: bogus")

        with pytest.raises(ValueError) as info:
            expand_program_call("program call 'bad.p3dat'", run)
        assert "bogus command" in str(info.value)
        assert "bad.p3dat" in str(info.value)

    def test_snippet_in_cycle_gap_resolves_against_cwd_not_task_file(self, tmp_path):
        # A task is cycling inside sub/outer.p3dat -> sub/inner.p3dat when
        # an execute_code snippet arrives through the cycle-gap callback
        # and issues its own top-level `program call 'probe.p3dat'`. The
        # snippet runs under its own context id; its call must resolve
        # against the working directory (like the engine would for a
        # top-level call), not against sub/ where the task currently is.
        from itasca_mcp_bridge.signals.interrupt import clear_current_task, set_current_task

        _write(tmp_path / "sub" / "outer.p3dat", "program call 'inner.p3dat'\nball create id 2\n")
        _write(tmp_path / "sub" / "inner.p3dat", "model cycle 10\n")
        _write(tmp_path / "probe.p3dat", "ball list ; top-level probe\n")
        _write(tmp_path / "sub" / "probe.p3dat", "ball list ; WRONG: task-file-relative probe\n")
        task_ran, snippet_ran = [], []

        def task_run(cmd):
            if expand_program_call(cmd, task_run):
                return
            task_ran.append(cmd)
            if cmd.startswith("model cycle"):
                # cycle-gap callback: snippet runs under its request id
                set_current_task("req-1")
                try:
                    if not expand_program_call("program call 'probe.p3dat'", snippet_ran.append):
                        snippet_ran.append("passthrough")
                finally:
                    set_current_task("task-1")

        set_current_task("task-1")
        try:
            assert expand_program_call("program call 'sub/outer.p3dat'", task_run) is True
        finally:
            clear_current_task()

        assert task_ran == ["model cycle 10", "ball create id 2"]
        assert snippet_ran == ["ball list ; top-level probe"]

    def test_depth_guard_passes_through(self, tmp_path, monkeypatch):
        _write(tmp_path / "self.p3dat", "program call 'self.p3dat'\n")
        monkeypatch.setattr(pc, "MAX_DEPTH", 3)
        passthrough = []

        def run(cmd):
            if not expand_program_call(cmd, run):
                passthrough.append(cmd)

        assert expand_program_call("program call 'self.p3dat'", run) is True
        # The engine gets the call once the guard trips, instead of recursing forever.
        assert passthrough == ["program call 'self.p3dat'"]

    def test_wrapped_command_integration(self, tmp_path):
        # Through the real wrapper: each file line is an itasca.command
        # boundary, so the model-reset repair hook fires for `model new`
        # inside the file.
        from itasca_mcp_bridge.signals.interrupt import register_interrupt_callback

        class Fake:
            def __init__(self):
                self.commands = []
                self.set_calls = []

            def set_callback(self, name, position):
                self.set_calls.append(name)

            def remove_callback(self, name, position):
                pass

            def command(self, cmd):
                self.commands.append(cmd)

        fake = Fake()
        assert register_interrupt_callback(fake) is True
        _write(tmp_path / "run.p3dat", "model new\nmodel cycle 10\n")
        base = fake.set_calls.count("_pfc_interrupt_check")
        fake.command("program call 'run.p3dat'")
        assert fake.commands == ["model new", "model cycle 10"]
        assert fake.set_calls.count("_pfc_interrupt_check") == base + 1


class TestOutputFidelity:
    def test_comments_echoed_in_place(self, tmp_path, capsys):
        _write(tmp_path / "c.p3dat", "; stage 1\nball create id 1\n; stage 2\nball create id 2\n")
        ran = []
        expand_program_call("program call 'c.p3dat'", ran.append)
        assert ran == ["ball create id 1", "ball create id 2"]
        out = capsys.readouterr().out.splitlines()
        assert out[0].startswith("[bridge] program call 'c.p3dat': 2 command(s)")
        assert out[1:] == ["; stage 1", "; stage 2"]

    def test_header_and_comments_printed_with_capture_paused(self, tmp_path, monkeypatch):
        # PFC 6 logs console prints into the live `program log` session,
        # so bridge/comment lines must be printed while the session is
        # paused or the task log gets them twice.
        from contextlib import contextmanager

        _write(tmp_path / "c.p3dat", "; stage 1\nball create id 1\n; stage 2\n")
        events = []

        @contextmanager
        def paused():
            events.append("pause")
            yield True
            events.append("resume")

        monkeypatch.setattr(pc, "live_capture_paused", paused)
        monkeypatch.setattr(pc, "flush_live_capture", lambda: events.append("flush"))
        monkeypatch.setattr(pc, "print", lambda line: events.append(line), raising=False)
        expand_program_call("program call 'c.p3dat'", lambda c: events.append(c))
        assert events == [
            "pause",
            "[bridge] program call 'c.p3dat': 1 command(s) run inline",
            "resume",
            "pause",
            "; stage 1",
            "resume",
            "ball create id 1",
            "flush",
            "pause",
            "; stage 2",
            "resume",
        ]

    def test_capture_flushed_after_each_command(self, tmp_path, monkeypatch):
        _write(tmp_path / "f.p3dat", "ball create id 1\nball create id 2\n")
        events = []
        monkeypatch.setattr(pc, "flush_live_capture", lambda: events.append("flush"))
        expand_program_call("program call 'f.p3dat'", lambda c: events.append(c))
        assert events == ["ball create id 1", "flush", "ball create id 2", "flush"]

    def test_capture_flushed_even_when_command_fails(self, tmp_path, monkeypatch):
        _write(tmp_path / "f.p3dat", "bogus\n")
        events = []
        monkeypatch.setattr(pc, "flush_live_capture", lambda: events.append("flush"))

        def run(cmd):
            raise ValueError("no")

        with pytest.raises(ValueError):
            expand_program_call("program call 'f.p3dat'", run)
        assert events == ["flush"]
