"""Tests for utils.command_log.capture_pfc_console — the itasca.command()
console-output capture, including the nested-scope path (#28).

Uses a fake `itasca` module that simulates the parts of ITASCA's
`program log` machinery the capture relies on: log-file switching (ending
a live session), truncate-vs-append on `log on`, and command echo +
output written only while logging is on. `model cycle` fires a registered
hook mid-command, emulating PFC's cycle-gap callback — the window where a
snippet's nested capture scope runs inside the task's.
"""

from __future__ import annotations

import sys
import types
from io import StringIO

import pytest
from itasca_mcp_bridge.utils import command_log
from itasca_mcp_bridge.utils.command_log import capture_pfc_console


class FakeItascaLog:
    """Simulate ITASCA's console logging just faithfully enough for #28.

    - `program log-file '<path>'`: if a session is live, append the
      logging-ended banner to the current file and end the session, then
      switch the file path.
    - `program log on truncate ...`: truncate the current file, session on.
    - `program log on ...` (no truncate): session on, append mode.
    - `program log off`: append the echo (the footer the capture strips),
      session off.
    - any other command: while a session is live, append `pfc3d><cmd>`
      plus the command's configured output. For `model cycle`, fire
      `cycle_hook` mid-command, then append the post-hook output — output
      the engine produces *after* the interleaved snippet ran.
    """

    def __init__(self):
        self.log_path = None
        self.logging = False
        self.outputs = {}
        self.post_hook_outputs = {}
        self.cycle_hook = None

    def _append(self, text):
        if self.log_path is None:
            return
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(text)

    def command(self, cmd):
        if cmd.startswith("program log-file"):
            if self.logging:
                self._append("* Logging ended\n")
                self.logging = False
            self.log_path = cmd.split("'")[1]
            return
        if cmd.startswith("program log on"):
            if "truncate" in cmd and self.log_path is not None:
                open(self.log_path, "w").close()
            self.logging = True
            return
        if cmd.startswith("program log off"):
            self._append("pfc3d>program log off\n")
            self.logging = False
            return
        if self.logging:
            self._append("pfc3d>{}\n".format(cmd))
            out = self.outputs.get(cmd)
            if out:
                self._append(out)
        if cmd.startswith("model cycle") and self.cycle_hook is not None:
            self.cycle_hook()
        if self.logging:
            post = self.post_hook_outputs.get(cmd)
            if post:
                self._append(post)


@pytest.fixture
def fake_itasca():
    fake = FakeItascaLog()
    module = types.ModuleType("itasca")
    module.command = fake.command
    sys.modules["itasca"] = module
    try:
        yield fake
    finally:
        sys.modules.pop("itasca", None)


@pytest.fixture(autouse=True)
def _reset_capture_state():
    # A test failing mid-scope must not leak the patch/stack into the next.
    yield
    getattr(command_log, "_stack", []).clear()
    if hasattr(command_log, "_orig_command"):
        command_log._orig_command = None


class TestSingleScope:
    def test_command_output_reaches_sink(self, fake_itasca, tmp_path):
        fake_itasca.outputs["ball list"] = "  Ball  Radius\n     1  0.25\n"
        sink = StringIO()

        with capture_pfc_console(sink, str(tmp_path)):
            import itasca

            itasca.command("ball list")

        assert "ball list" in sink.getvalue()
        assert "1  0.25" in sink.getvalue()

    def test_patch_restored_and_stack_empty_after_exit(self, fake_itasca, tmp_path):
        with capture_pfc_console(StringIO(), str(tmp_path)):
            import itasca

            assert itasca.command is command_log._patched

        import itasca

        # Bound methods are compared by ==, not `is`: each attribute
        # access on fake_itasca creates a fresh bound-method object.
        assert itasca.command == fake_itasca.command
        assert command_log._stack == []
        assert command_log._orig_command is None

    def test_user_command_error_propagates_and_restores(self, fake_itasca, tmp_path):
        real_command = fake_itasca.command

        def exploding(cmd):
            if cmd == "bad command":
                raise ValueError("unknown command")
            real_command(cmd)

        fake_itasca.command = exploding
        sys.modules["itasca"].command = exploding

        with pytest.raises(ValueError):
            with capture_pfc_console(StringIO(), str(tmp_path)):
                import itasca

                itasca.command("bad command")

        import itasca

        assert itasca.command is exploding
        assert command_log._stack == []


class TestNestedScope:
    """The #28 scenario: a snippet's capture scope opens inside a cycling
    task's scope via the cycle-gap callback. Pre-fix, the inner scope
    grabbed the outer wrapper as its "original" and the re-wrapped
    `program log on truncate` wiped the inner log file before it was
    read — the inner capture came back empty."""

    def test_inner_capture_gets_its_command_output(self, fake_itasca, tmp_path):
        fake_itasca.outputs["model cycle 100"] = "cycle table header\n"
        fake_itasca.outputs["ball list"] = "  Ball  Radius\n     1  0.25\n"
        outer_sink, inner_sink = StringIO(), StringIO()

        def snippet_at_cycle_gap():
            with capture_pfc_console(inner_sink, str(tmp_path)):
                import itasca

                itasca.command("ball list")

        fake_itasca.cycle_hook = snippet_at_cycle_gap

        with capture_pfc_console(outer_sink, str(tmp_path)):
            import itasca

            itasca.command("model cycle 100")

        assert "1  0.25" in inner_sink.getvalue()

    def test_outer_session_resumes_after_inner_exit(self, fake_itasca, tmp_path):
        # Output the engine produces after the interleaved snippet must
        # still reach the outer sink: the inner scope's exit restores the
        # outer log file and resumes its interrupted session in append
        # mode, so the pre-snippet output survives alongside it.
        fake_itasca.outputs["model cycle 100"] = "pre-snippet cycle output\n"
        fake_itasca.post_hook_outputs["model cycle 100"] = "post-snippet cycle output\n"
        outer_sink = StringIO()
        restored_paths = []

        def snippet_at_cycle_gap():
            with capture_pfc_console(StringIO(), str(tmp_path)):
                import itasca

                itasca.command("ball list")
            restored_paths.append(fake_itasca.log_path)

        fake_itasca.cycle_hook = snippet_at_cycle_gap

        with capture_pfc_console(outer_sink, str(tmp_path)):
            import itasca

            itasca.command("model cycle 100")

        assert "pre-snippet cycle output" in outer_sink.getvalue()
        assert "post-snippet cycle output" in outer_sink.getvalue()
        # After the inner scope exits, ITASCA's log file must point back
        # at the outer scope's file (not the inner's deleted one).
        assert len(restored_paths) == 1
        assert "cmdtmp_" in restored_paths[0]
        assert fake_itasca.logging is False  # outer's log off ran at the end

    def test_inner_output_does_not_leak_into_outer_sink(self, fake_itasca, tmp_path):
        fake_itasca.outputs["ball list"] = "inner-only table\n"
        outer_sink, inner_sink = StringIO(), StringIO()

        def snippet_at_cycle_gap():
            with capture_pfc_console(inner_sink, str(tmp_path)):
                import itasca

                itasca.command("ball list")

        fake_itasca.cycle_hook = snippet_at_cycle_gap

        with capture_pfc_console(outer_sink, str(tmp_path)):
            import itasca

            itasca.command("model cycle 100")

        assert "inner-only table" in inner_sink.getvalue()
        assert "inner-only table" not in outer_sink.getvalue()

    def test_patch_survives_until_outermost_exit(self, fake_itasca, tmp_path):
        states = {}

        def snippet_at_cycle_gap():
            with capture_pfc_console(StringIO(), str(tmp_path)):
                pass
            import itasca

            # Inner exit must NOT unpatch: the outer scope still needs it.
            states["patched_after_inner_exit"] = (
                itasca.command is command_log._patched
            )

        fake_itasca.cycle_hook = snippet_at_cycle_gap

        with capture_pfc_console(StringIO(), str(tmp_path)):
            import itasca

            itasca.command("model cycle 100")

        import itasca

        assert states["patched_after_inner_exit"] is True
        assert itasca.command == fake_itasca.command
