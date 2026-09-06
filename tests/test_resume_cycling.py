"""Tests for the cycling resume after a callback command error.

PFC 6 aborts the cycling a callback interrupted when an engine command
fails inside that callback: a mistyped execute_code command sent while a
task cycles silently cuts the task's running `model cycle` / `model solve`
short (verified live on 6.00.030, 2026-09-06; PFC 7 keeps cycling). The
wrapped itasca.command records the failure and, when the outer cycling
command returns at that very cycle, re-issues the remainder.
"""

from __future__ import annotations

import re

import pytest
from itasca_mcp_bridge.signals import cycle_executor
from itasca_mcp_bridge.signals import interrupt as interrupt_mod
from itasca_mcp_bridge.signals.interrupt import (
    _resume_command,
    clear_current_task,
    clear_interrupt,
    register_interrupt_callback,
    request_interrupt,
    set_current_task,
)

from test_interrupt import _FakeItasca


class _CyclingFakeItasca:
    """Engine stand-in that actually cycles.

    `model cycle N` and `model solve ... cycles N` advance the counter
    one step at a time. At each cycle listed in `fail_at` the fake does
    what the executor callback does for an execute_code snippet whose
    engine command is mistyped: runs it through the wrapped
    itasca.command with the callback depth raised, and swallows the
    error the way a snippet with try/except would. `aborts=True`
    emulates PFC 6 (the engine stops cycling at that gap);
    `aborts=False` emulates PFC 7 (cycling carries on).
    """

    _RUN_RE = re.compile(r"^model (cycle|solve)\b(.*)$")
    _COUNT_RE = re.compile(r"(?<![\w-])cycles? (\d+)")

    def __init__(self, aborts=True, fail_at=()):
        self.aborts = aborts
        self.fail_at = list(fail_at)
        self.on_failure = None  # optional hook run inside the callback
        self.commands: list[str] = []
        self._cycle = 0

    def set_callback(self, name, position):
        pass

    def remove_callback(self, name, position):
        pass

    def cycle(self):
        return self._cycle

    def command(self, cmd):
        # The raw engine. Registration replaces the attribute with the
        # wrapper, so `self.command(...)` below goes through the wrapper.
        self.commands.append(cmd)
        if cmd == "bad command":
            raise RuntimeError("Error in execution - See the Itasca Console")
        m = self._RUN_RE.match(cmd)
        if not m:
            return
        if m.group(1) == "cycle":
            steps = int(m.group(2).split()[0])
        else:
            count = self._COUNT_RE.search(m.group(2))
            steps = int(count.group(1)) if count else 1000
        for _ in range(steps):
            self._cycle += 1
            if self._cycle in self.fail_at:
                self.fail_at.remove(self._cycle)
                self._snippet_command_fails()
                if self.aborts:
                    return

    def _snippet_command_fails(self):
        cycle_executor._callback_depth[0] += 1
        try:
            try:
                self.command("bad command")
            except RuntimeError:
                pass
            if self.on_failure is not None:
                self.on_failure()
        finally:
            cycle_executor._callback_depth[0] -= 1


def _registered(**kw) -> _CyclingFakeItasca:
    fake = _CyclingFakeItasca(**kw)
    assert register_interrupt_callback(fake) is True
    return fake


class TestResumeAfterCallbackCommandError:
    def test_pfc6_abort_resumes_model_cycle(self, capsys):
        fake = _registered(aborts=True, fail_at=[200])
        fake.command("model cycle 500")
        assert fake.commands == ["model cycle 500", "bad command", "model cycle 300"]
        assert fake.cycle() == 500
        out = capsys.readouterr().out
        assert "[bridge] cycling stopped at cycle 200" in out
        assert "resuming with `model cycle 300`" in out
        assert "`bad command`" in out

    def test_pfc7_keeps_cycling_nothing_reissued(self, capsys):
        fake = _registered(aborts=False, fail_at=[200])
        fake.command("model cycle 500")
        assert fake.commands == ["model cycle 500", "bad command"]
        assert fake.cycle() == 500
        assert "[bridge]" not in capsys.readouterr().out

    def test_repeated_abort_resumes_again(self):
        fake = _registered(aborts=True, fail_at=[200, 350])
        fake.command("model cycle 500")
        assert fake.commands == [
            "model cycle 500",
            "bad command",
            "model cycle 300",
            "bad command",
            "model cycle 150",
        ]
        assert fake.cycle() == 500

    def test_solve_with_absolute_limits_reissued_verbatim(self):
        fake = _registered(aborts=True, fail_at=[300])
        fake.command("model solve ratio 1e-5 cycles-total 5000")
        assert fake.commands == [
            "model solve ratio 1e-5 cycles-total 5000",
            "bad command",
            "model solve ratio 1e-5 cycles-total 5000",
        ]

    def test_solve_relative_cycles_rewritten_to_remainder(self):
        fake = _registered(aborts=True, fail_at=[400])
        fake.command("model solve ratio 1e-5 cycles 1000")
        assert fake.commands == [
            "model solve ratio 1e-5 cycles 1000",
            "bad command",
            "model solve ratio 1e-5 cycles 600",
        ]
        assert fake.cycle() == 1000

    def test_solve_with_time_limit_is_not_resumed(self, capsys):
        fake = _registered(aborts=True, fail_at=[300])
        fake.command("model solve time 2.0")
        assert fake.commands == ["model solve time 2.0", "bad command"]
        out = capsys.readouterr().out
        assert "[bridge] cycling stopped at cycle 300" in out
        assert "Not resumed" in out

    def test_pending_interrupt_wins_over_resume(self):
        fake = _registered(aborts=True, fail_at=[200])
        fake.on_failure = lambda: request_interrupt("t1")
        set_current_task("t1")
        try:
            with pytest.raises(InterruptedError):
                fake.command("model cycle 500")
            assert fake.commands == ["model cycle 500", "bad command"]
        finally:
            clear_interrupt("t1")
            clear_current_task()

    def test_stale_failure_does_not_touch_a_later_command(self):
        fake = _registered(aborts=True, fail_at=[200])
        fake.command("model cycle 500")
        assert fake.commands[-1] == "model cycle 300"
        fake.command("model cycle 100")  # no failure during this one
        assert fake.commands[-1] == "model cycle 100"
        assert fake.cycle() == 600

    def test_non_cycling_command_is_never_resumed(self):
        fake = _registered(aborts=True)
        interrupt_mod._callback_failure_seq += 1  # a stale record
        interrupt_mod._callback_failure_cycle = fake.cycle()
        fake.command("ball list")
        assert fake.commands == ["ball list"]

    def test_failure_outside_callback_is_not_recorded(self):
        fake = _registered()
        before = interrupt_mod._callback_failure_seq
        with pytest.raises(RuntimeError) as info:
            fake.command("bad command")
        assert interrupt_mod._callback_failure_seq == before
        assert "[bridge]" not in str(info.value)

    def test_failure_inside_callback_annotates_the_error(self, monkeypatch):
        fake = _registered()
        monkeypatch.setattr(cycle_executor, "_callback_depth", [1])
        with pytest.raises(RuntimeError) as info:
            fake.command("bad command")
        assert "failed inside the cycle callback" in str(info.value)
        assert "PFC 6" in str(info.value)

    def test_interrupt_inside_callback_is_not_a_failure(self, monkeypatch):
        fake = _CyclingFakeItasca()

        def interrupted_engine(cmd):
            raise InterruptedError("stop")

        fake.command = interrupted_engine
        assert register_interrupt_callback(fake) is True
        monkeypatch.setattr(cycle_executor, "_callback_depth", [1])
        before = interrupt_mod._callback_failure_seq
        with pytest.raises(InterruptedError):
            fake.command("ball list")
        assert interrupt_mod._callback_failure_seq == before

    def test_engine_without_cycle_count_never_resumes(self):
        # _FakeItasca has no cycle(): the wrapper cannot tell an abort
        # from a normal return and must leave the command alone.
        fake = _FakeItasca()

        def engine(cmd):
            _FakeItasca.command(fake, cmd)
            if cmd == "bad command":
                raise RuntimeError("engine error")
            if cmd == "model cycle 10":
                cycle_executor._callback_depth[0] += 1
                try:
                    try:
                        fake.command("bad command")
                    except RuntimeError:
                        pass
                finally:
                    cycle_executor._callback_depth[0] -= 1

        fake.command = engine
        assert register_interrupt_callback(fake) is True
        fake.command("model cycle 10")
        assert fake.commands == ["model cycle 10", "bad command"]


class TestResumeCommand:
    def test_model_cycle_keeps_abbreviation_and_tail(self):
        assert _resume_command("mod cyc 100 calm 10", 40) == "mod cyc 60 calm 10"
        assert _resume_command("  model cycle 100", 1) == "  model cycle 99"

    def test_model_cycle_exhausted(self):
        assert _resume_command("model cycle 100", 100) is None
        assert _resume_command("model cycle 100", 150) is None

    def test_solve_rewrites_every_relative_cycles_limit(self):
        assert (
            _resume_command("model solve mechanical cycles 100 thermal cyc 50", 30)
            == "model solve mechanical cycles 70 thermal cyc 20"
        )

    def test_solve_relative_cycles_exhausted(self):
        assert _resume_command("model solve ratio 1e-5 cycles 100", 100) is None

    def test_solve_absolute_limits_untouched(self):
        cmd = "model solve ratio 1e-5 cycles-total 5000 time-total 3.0"
        assert _resume_command(cmd, 123) == cmd

    def test_solve_unrewritable_limits(self):
        assert _resume_command("model solve time 1.0", 5) is None
        assert _resume_command("model solve clock 30", 5) is None
        assert _resume_command("model solve elastic", 5) is None
        assert _resume_command("model solve ratio 1e-5 el only", 5) is None

    def test_not_a_cycling_command(self):
        assert _resume_command("ball list", 5) is None
        assert _resume_command("model save 'x'", 5) is None
        assert _resume_command("model cycle", 5) is None
