"""Tests for the autostart hook — starting the bridge with the product.

The hook runs during interpreter startup inside someone else's GUI, so the
tests here are mostly about the ways it must *not* act: a plain interpreter
that happens to live under a product directory (the self-upgrade runs pip
with exactly that), a console build with no event loop, a second product
starting when one bridge is already listening, and a sitecustomize.py that
belongs to somebody else.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from itasca_mcp_bridge import autostart


# ---- which interpreters may arm ---------------------------------------

# Built with the platform's own separator rather than written as Windows
# literals: `os.path.basename` does not treat `\` as one on POSIX, so a
# literal `D:\...\python.exe` is its own basename there and the negative
# cases below would invert on CI. The real spelling on this machine is
# `D:\Program Files\Itasca\PFC700\exe64\pfc2d700_gui.exe` next to
# `...\exe64\python36\python.exe`.
ENGINE = os.path.join("Itasca", "PFC700", "exe64")
EMBEDDED_PYTHON = os.path.join(ENGINE, "python36", "python.exe")


@pytest.mark.parametrize(
    "executable",
    [
        os.path.join(ENGINE, "pfc2d700_gui.exe"),
        os.path.join("Itasca", "FLAC3D700", "exe64", "flac3d700_gui.exe"),
        os.path.join("Itasca", "3DEC700", "3dec700_console"),
        os.path.join("Itasca", "MPoint700", "mpoint_gui.exe"),
    ],
)
def test_engine_binaries_arm(executable):
    assert autostart.is_engine_interpreter(executable) is True


@pytest.mark.parametrize(
    "executable",
    [
        # The package's own self-upgrade runs pip with this one. A substring
        # test on the whole path would match "PFC700" and have a process that
        # is about to exit poll for two minutes.
        EMBEDDED_PYTHON,
        os.path.join("Python36", "python.exe"),
        os.path.join(os.sep, "usr", "bin", "python3"),
        os.path.join("tools", "my_runner.exe"),
    ],
)
def test_plain_interpreters_do_not_arm(executable):
    assert autostart.is_engine_interpreter(executable) is False


def test_engine_hints_are_overridable(monkeypatch):
    monkeypatch.setenv(autostart.ENV_ENGINE_HINTS, "itascasoft")
    assert autostart.is_engine_interpreter("itascasoft_gui.exe") is True
    assert autostart.is_engine_interpreter("pfc2d700_gui.exe") is False


# ---- boot() decides, and does not raise -------------------------------


def test_boot_declines_on_a_plain_interpreter(monkeypatch):
    started = []
    monkeypatch.setattr(autostart.threading, "Thread", lambda **kw: started.append(kw))
    monkeypatch.setattr(autostart.sys, "executable", "python.exe")
    assert autostart.boot() is False
    assert started == []


def test_boot_declines_when_a_bridge_is_already_listening(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setattr(autostart.sys, "executable", "pfc2d700_gui.exe")
    monkeypatch.setattr(autostart, "port_in_use", lambda host, port, timeout=0.3: True)
    monkeypatch.setattr(autostart.threading, "Thread", lambda **kw: started.append(kw))

    assert autostart.boot() is False
    assert started == []
    assert "already in use" in (tmp_path / "autostart.log").read_text()


def test_boot_arms_a_daemon_thread(monkeypatch, tmp_path):
    created = {}

    class _Thread:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def start(self):
            created["started"] = True

        def __setattr__(self, name, value):
            created[name] = value

    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setattr(autostart.sys, "executable", "pfc2d700_gui.exe")
    monkeypatch.setattr(autostart, "port_in_use", lambda host, port, timeout=0.3: False)
    monkeypatch.setattr(autostart.threading, "Thread", _Thread)

    assert autostart.boot() is True
    assert created["started"] is True
    assert created["daemon"] is True
    assert created["name"] == "mcp-bridge-autostart"


def test_boot_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setattr(autostart.sys, "executable", "pfc2d700_gui.exe")

    def _explode(name, default):
        raise RuntimeError("bad environment")

    monkeypatch.setattr(autostart, "_env_float", _explode)
    assert autostart.boot() is False


# ---- the notice window ------------------------------------------------


class _FakeButton:
    """One button on a dialog. Clicking the right one is what dismisses it."""

    def __init__(self, text, owner, visible=True, dismisses=True):
        self._text = text
        self._owner = owner
        self._visible = visible
        self._dismisses = dismisses
        self.clicks = 0

    def text(self):
        return self._text

    def isVisible(self):
        return self._visible

    def click(self):
        self.clicks += 1
        if self._dismisses:
            self._owner._visible = False


class _FakeLabel:
    """A child label. This is where a plain `QWidget` keeps its body text."""

    def __init__(self, text, visible=True):
        self._text = text
        self._visible = visible

    def text(self):
        return self._text

    def isVisible(self):
        return self._visible


class _FakeWidget:
    def __init__(self, title, visible=True, refuses=False, buttons=(), labels=()):
        self._title = title
        self._visible = visible
        self._refuses = refuses
        self.buttons = list(buttons)
        self.labels = list(labels)
        self.closes = 0

    def windowTitle(self):
        return self._title

    def isVisible(self):
        return self._visible

    def close(self):
        self.closes += 1
        if not self._refuses:
            self._visible = False

    def findChildren(self, kind):
        if kind is _FakeButton:
            return list(self.buttons)
        if kind is _FakeLabel:
            return list(self.labels)
        return []


class _FakeDialog(_FakeWidget):
    """A widget that asks something. `QDialog` is the hook's only test for it."""


class _FakeMessageBox(_FakeDialog):
    """A dialog that carries its body in `text()`, the way `QMessageBox` does."""

    def __init__(self, title, body, **kwargs):
        _FakeDialog.__init__(self, title, **kwargs)
        self._body = body

    def text(self):
        return self._body


def _dialog(title, *labels, **kwargs):
    """A dialog carrying buttons, so the sweep has something to read."""
    dialog = _FakeDialog(title, **kwargs)
    dialog.buttons = [_FakeButton(label, dialog) for label in labels]
    return dialog


class _FakeApplication:
    def __init__(self, widgets):
        self._widgets = widgets

    def topLevelWidgets(self):
        return self._widgets


class _FakeWidgets:
    QDialog = _FakeDialog
    QAbstractButton = _FakeButton
    QLabel = _FakeLabel
    QApplication = None

    def __init__(self, widgets):
        self.QApplication = _FakeApplication(widgets)


def _with_widgets(monkeypatch, widgets):
    monkeypatch.setattr(autostart, "_qt_widgets", lambda: _FakeWidgets(widgets))
    monkeypatch.setattr(autostart, "_reported_dialogs", set())
    monkeypatch.setattr(autostart, "_attempted_dismissals", set())
    monkeypatch.setattr(autostart, "_dialogs", [])
    monkeypatch.setattr(autostart, "_dialog_ids", {})
    monkeypatch.setattr(autostart, "_pending_answer", None)
    monkeypatch.setattr(autostart, "_answer_results", {})


def test_closes_only_the_revision_notice(monkeypatch):
    notice = _FakeWidget("PFC2D 7.00.161 : Startup")
    document = _FakeWidget("Model - PFC2D 7.00.161")
    _with_widgets(monkeypatch, [notice, document])

    assert autostart.close_notice_windows() == ["PFC2D 7.00.161 : Startup"]
    assert notice.closes == 1
    assert document.closes == 0


def test_a_notice_qt_refused_is_not_reported_as_closed(monkeypatch):
    # Qt will not close a widget sitting inside a modal exec_(), and close()
    # returns without raising, so the attempt alone proves nothing.
    notice = _FakeWidget("PFC2D 7.00.161 : Startup", refuses=True)
    _with_widgets(monkeypatch, [notice])

    assert autostart.close_notice_windows() == []
    assert notice.closes == 1


def test_hidden_windows_are_skipped(monkeypatch):
    notice = _FakeWidget("PFC2D 7.00.161 : Startup", visible=False)
    _with_widgets(monkeypatch, [notice])

    assert autostart.close_notice_windows() == []
    assert notice.closes == 0


def test_a_broken_widget_does_not_stop_the_sweep(monkeypatch):
    class _Broken:
        def isVisible(self):
            raise RuntimeError("no")

    notice = _FakeWidget("PFC2D 7.00.161 : Startup")
    _with_widgets(monkeypatch, [_Broken(), notice])

    assert autostart.close_notice_windows() == ["PFC2D 7.00.161 : Startup"]


def test_no_qt_binding_is_not_an_error(monkeypatch):
    monkeypatch.setattr(autostart, "_qt_widgets", lambda: None)
    assert autostart.close_notice_windows() == []


def test_notice_closing_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv(autostart.ENV_DISMISS_WINDOWS, raising=False)
    assert autostart._env_flag(autostart.ENV_DISMISS_WINDOWS, False) is False
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    assert autostart._env_flag(autostart.ENV_DISMISS_WINDOWS, False) is True
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "off")
    assert autostart._env_flag(autostart.ENV_DISMISS_WINDOWS, False) is False


# ---- dialogs the hook will not answer ---------------------------------


def test_a_dialog_is_reported_and_the_window_behind_it_is_not(monkeypatch):
    dialog = _FakeDialog("Recover Project File")
    document = _FakeWidget("Model - PFC2D 7.00.161")
    _with_widgets(monkeypatch, [dialog, document])

    # A plain top-level window is not a question, and reporting it would
    # bury the one that is.
    assert autostart.waiting_dialogs() == ["Recover Project File"]
    # Reported, not answered: this one is offering a choice.
    assert dialog.closes == 0


def test_the_startup_notice_is_not_a_waiting_dialog(monkeypatch):
    _with_widgets(monkeypatch, [_FakeDialog("PFC2D 7.00.161 : Startup")])
    assert autostart.waiting_dialogs() == []


def test_hidden_dialogs_are_not_waiting(monkeypatch):
    # A dialog the product has already dismissed is not holding anything.
    _with_widgets(monkeypatch, [_FakeDialog("Recover Project File", visible=False)])
    assert autostart.waiting_dialogs() == []


def _log_of(tmp_path):
    # "" when nothing was ever written, which is a real outcome and not a
    # missing fixture: the log file is created by the first line that goes
    # into it, so "no file" and "no lines" are the same statement.
    path = tmp_path / "autostart.log"
    return path.read_text() if path.exists() else ""


def test_a_waiting_dialog_is_logged_once_not_once_a_second(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    _with_widgets(monkeypatch, [_FakeDialog("Recover Project File")])

    autostart._tick_windows()
    autostart._tick_windows()
    autostart._tick_windows()

    # The dialog is still up on every tick. A log that says so thirty times a
    # minute is a log nobody reads, which is the same as no log.
    assert _log_of(tmp_path).count("Recover Project File") == 1


def test_a_dialog_is_reported_even_when_closing_is_turned_off(monkeypatch, tmp_path):
    # The two halves of the pass are independent: this is the one that has to
    # survive on a default install, because it is the only symptom of a
    # bridge whose HTTP server answers while every task hangs.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.delenv(autostart.ENV_DISMISS_WINDOWS, raising=False)
    notice = _FakeWidget("PFC2D 7.00.161 : Startup")
    _with_widgets(monkeypatch, [notice, _FakeDialog("Recover Project File")])

    autostart._tick_windows()

    assert "Recover Project File" in _log_of(tmp_path)
    assert "closed the product's notice window" not in _log_of(tmp_path)
    assert notice.closes == 0


def test_closing_is_the_opt_in_half_of_the_same_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    notice = _FakeWidget("PFC2D 7.00.161 : Startup")
    _with_widgets(monkeypatch, [notice])

    autostart._tick_windows()

    assert notice.closes == 1
    assert "closed the product's notice window: PFC2D 7.00.161 : Startup" in _log_of(tmp_path)


# ---- dialogs that ask nothing -----------------------------------------


def test_a_box_with_one_ok_is_answered(monkeypatch):
    # Measured on PFC2D 7.00.161: this is the box that blocks the product and
    # offers no way past it.
    dialog = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == ["PFC2D 7.00"]
    assert dialog.buttons[0].clicks == 1


def test_a_box_that_offers_a_choice_is_left_standing(monkeypatch):
    # `close()` cannot dismiss these -- Qt refuses inside a modal exec_() --
    # and answering one means picking for somebody. So they stay.
    dialog = _dialog("Recover Project File", "Open", "Discard")
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == []
    assert dialog.buttons[0].clicks == 0
    assert dialog.buttons[1].clicks == 0


def test_a_yes_is_a_choice_even_with_no_visible_no(monkeypatch):
    # "Yes" implies a "No" exists somewhere, so it is not in the
    # acknowledgement set even when this particular box does not draw it.
    dialog = _dialog("Restore save file initial.sav?", "Yes", "No")
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == []
    assert dialog.buttons[0].clicks == 0


def test_an_ok_cancel_box_is_a_choice(monkeypatch):
    dialog = _dialog("Are you sure you want to restore save file initial.sav?", "OK", "Cancel")
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == []
    assert dialog.buttons[0].clicks == 0


def test_the_notice_is_not_a_dialog_to_answer(monkeypatch):
    # It is closed by the other half of the pass; answering it here would
    # have the same window logged twice under two different verbs.
    notice = _dialog("PFC2D 7.00.161 : Startup", "Ok")
    _with_widgets(monkeypatch, [notice])

    assert autostart.dismiss_dialogs() == []
    assert notice.buttons[0].clicks == 0


def test_a_box_with_no_visible_buttons_is_not_answered(monkeypatch):
    # Nothing to click means nothing to read: an empty label set is not the
    # same as a set of acknowledgements, and treating it as one would have
    # the hook guessing at a window it cannot see into.
    hidden = _dialog("PFC2D 7.00", "Ok")
    hidden.buttons[0]._visible = False
    _with_widgets(monkeypatch, [hidden])

    assert autostart.dismiss_dialogs() == []


def test_a_click_that_does_not_dismiss_is_not_reported_as_one(monkeypatch):
    # Real Qt can refuse. `isVisible()` is re-read instead of the click
    # being trusted, exactly as the notice sweep does.
    dialog = _dialog("PFC2D 7.00", "Ok")
    dialog.buttons[0]._dismisses = False
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == []
    assert dialog.buttons[0].clicks == 1


def test_a_refusing_button_is_clicked_once_not_once_a_second(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    dialog = _dialog("PFC2D 7.00", "Ok")
    dialog.buttons[0]._dismisses = False
    _with_widgets(monkeypatch, [dialog])

    autostart._tick_windows()
    autostart._tick_windows()
    autostart._tick_windows()

    assert dialog.buttons[0].clicks == 1
    # And what survives the click is reported, because it is still in the way.
    assert "a dialog is waiting for a human" in _log_of(tmp_path)


def test_a_chain_of_boxes_is_cleared_in_one_tick(monkeypatch, tmp_path):
    # Answering the first box on PFC2D 7.00.161 produced two more, so the
    # pass has to repeat. Each tick re-reads the widget list, so a queue
    # that grows while it is being drained still gets drained.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    first = _dialog("Recover Project File", "Open", "Discard")
    second = _dialog("Are you sure you want to restore save file initial.sav?", "OK", "Cancel")
    third = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [first, second, third])

    autostart._tick_windows()

    # The two that ask something are untouched; the one that does not is gone.
    assert first.buttons[0].clicks == 0
    assert second.buttons[0].clicks == 0
    assert third.buttons[0].clicks == 1
    # And the two that were answered by nobody are named in the log.
    log = _log_of(tmp_path)
    assert "Recover Project File" in log
    assert "save file initial.sav" in log


def test_answering_is_opt_in_and_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.delenv(autostart.ENV_DISMISS_WINDOWS, raising=False)
    dialog = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [dialog])

    autostart._tick_windows()

    assert dialog.buttons[0].clicks == 0
    assert "PFC2D 7.00" in _log_of(tmp_path)


# ---- what the product is asking, for a client that wants to decide ----

# The bridge's own policy is narrow on purpose: it answers a box that asks
# nothing and leaves every real choice alone. That policy needs a client to
# be optional rather than required, which is what these cover -- a snapshot
# of what is on screen, and a way to click a named button on it.


def test_the_snapshot_carries_what_a_client_needs_to_decide(monkeypatch):
    box = _FakeMessageBox(
        "PFC2D 7.00", "Model state is currently marked as unrepeatable.",
        labels=[_FakeLabel("Cycle 0")],
    )
    box.buttons = [_FakeButton("Ok", box)]
    _with_widgets(monkeypatch, [box])

    assert autostart.refresh_dialogs() == [
        {
            "id": 1,
            "title": "PFC2D 7.00",
            "text": "Model state is currently marked as unrepeatable.\nCycle 0",
            "buttons": ["Ok"],
            "asks_nothing": True,
        }
    ]


def test_a_dialogs_body_is_read_from_labels_when_it_has_no_text(monkeypatch):
    # The third box on PFC2D 7.00.161 is a plain QWidget, not a QMessageBox,
    # and it keeps its body in child labels. A snapshot that only knew about
    # text() would hand the client an empty string and no way to decide.
    box = _FakeDialog("PFC2D 7.00", labels=[_FakeLabel("unrepeatable")])
    box.buttons = [_FakeButton("Ok", box)]
    _with_widgets(monkeypatch, [box])

    assert autostart.refresh_dialogs()[0]["text"] == "unrepeatable"


def test_a_label_hidden_from_the_reader_is_hidden_from_the_client(monkeypatch):
    box = _FakeDialog("PFC2D 7.00", labels=[_FakeLabel("shown"), _FakeLabel("gone", visible=False)])
    box.buttons = [_FakeButton("Ok", box)]
    _with_widgets(monkeypatch, [box])

    assert autostart.refresh_dialogs()[0]["text"] == "shown"


def test_the_snapshot_leaves_out_windows_that_ask_nothing_of_anyone(monkeypatch):
    # An empty `dialogs` list is how a client learns the product is free.
    # Filling it with document windows and the revision notice would make
    # that question unanswerable.
    document = _FakeWidget("Model - PFC2D 7.00.161")
    notice = _dialog("PFC2D 7.00.161 : Startup", "Ok")
    _with_widgets(monkeypatch, [document, notice, _dialog("Recover Project File", "Open", "Discard")])

    assert [d["title"] for d in autostart.refresh_dialogs()] == ["Recover Project File"]


def test_an_id_stays_put_while_the_dialog_does(monkeypatch):
    # A client reads an id, thinks about it, and posts it back. Renumbering
    # between the two would have it answer whatever moved into that slot.
    box = _dialog("Recover Project File", "Open", "Discard")
    _with_widgets(monkeypatch, [box])

    assert autostart.refresh_dialogs()[0]["id"] == autostart.refresh_dialogs()[0]["id"]
    assert autostart.dialogs()[0]["id"] == 1


def test_the_snapshot_goes_stale_until_the_watch_refreshes_it(monkeypatch, tmp_path):
    # `dialogs()` is a read of the last snapshot, not a fresh look: it is
    # called from a request thread, and looking would mean touching widgets
    # from one.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    box = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [box])

    assert autostart.dialogs() == []
    autostart._tick_windows()
    assert [d["title"] for d in autostart.dialogs()] == ["PFC2D 7.00"]


def _post_and_drain(dialog_id, label, timeout=5.0):
    """Answer as a request thread does, with the test thread as the watch."""
    out = {}

    def _post():
        out["result"] = autostart.answer_dialog(dialog_id, label, timeout=timeout)

    thread = threading.Thread(target=_post)
    thread.start()
    for _ in range(400):
        if autostart._pending_answer is not None:
            break
        time.sleep(0.005)
    autostart._tick_windows()
    thread.join(5.0)
    return out.get("result"), thread


def test_a_client_can_answer_a_dialog_the_hook_would_leave_alone(monkeypatch, tmp_path):
    # The policy is the hook's, not the bridge's. Nothing refuses this one.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    box = _dialog("Recover Project File", "Open", "Discard")
    _with_widgets(monkeypatch, [box])
    autostart.refresh_dialogs()

    result, thread = _post_and_drain(1, "Open")

    assert thread.is_alive() is False
    assert result["status"] == "success"
    assert box.buttons[0].clicks == 1
    assert box.buttons[1].clicks == 0


def test_the_answer_is_carried_out_on_the_watch_and_clicked_once(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    box = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [box])
    autostart.refresh_dialogs()

    _post_and_drain(1, "Ok")
    autostart._tick_windows()
    autostart._tick_windows()

    assert box.buttons[0].clicks == 1


def test_a_button_the_dialog_does_not_have_is_refused(monkeypatch, tmp_path):
    # Matching against the buttons actually present is what stops an id that
    # has been reused from clicking whatever is now under it.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    box = _dialog("Recover Project File", "Open", "Discard")
    _with_widgets(monkeypatch, [box])
    autostart.refresh_dialogs()

    result, _ = _post_and_drain(1, "Delete Everything")

    assert result["status"] == "error"
    assert "no 'delete everything' button" in result["message"]
    # What it does offer, so the caller does not have to ask again to find out.
    assert "['Open', 'Discard']" in result["message"]
    assert box.buttons[0].clicks == 0
    assert box.buttons[1].clicks == 0


def test_answering_a_dialog_that_is_gone_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    box = _dialog("Recover Project File", "Open", "Discard")
    _with_widgets(monkeypatch, [box])
    autostart.refresh_dialogs()
    box._visible = False

    result, _ = _post_and_drain(1, "Open")

    assert result["status"] == "error"
    assert "not on screen any more" in result["message"]
    assert box.buttons[0].clicks == 0


def test_an_id_that_was_never_issued_says_that_instead(monkeypatch, tmp_path):
    # "not on screen any more" for an id that never existed sends the caller
    # looking for a dialog that was never there. Both are errors; only one of
    # them is true, and the message has to be the one that is.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    _with_widgets(monkeypatch, [_dialog("Recover Project File", "Open", "Discard")])
    autostart.refresh_dialogs()

    result, _ = _post_and_drain(7, "Open")

    assert result["status"] == "error"
    assert "there is no dialog with id 7" in result["message"]
    assert "[1]" in result["message"]
    assert "not on screen" not in result["message"]


def test_the_answer_reports_the_label_the_product_draws(monkeypatch, tmp_path):
    # The caller sends a button name; echoing it back lowercased reads as
    # though the product had renamed the button under it.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    box = _dialog("content probe", "OK", "Cancel")
    _with_widgets(monkeypatch, [box])
    autostart.refresh_dialogs()

    result, _ = _post_and_drain(1, "cancel")

    assert result["status"] == "success"
    assert result["message"] == "answered 'Cancel' on 'content probe'"
    assert box.buttons[1].clicks == 1


def test_a_click_the_product_ignores_is_not_reported_as_an_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    box = _dialog("PFC2D 7.00", "Ok")
    box.buttons[0]._dismisses = False
    _with_widgets(monkeypatch, [box])
    autostart.refresh_dialogs()

    result, _ = _post_and_drain(1, "Ok")

    assert result["status"] == "error"
    assert "did not act" in result["message"]


def test_an_answer_nobody_drains_is_withdrawn_and_reported(monkeypatch):
    # The failure this whole module is about: a queued call with nothing on
    # the other end looks exactly like one that worked. Here it must not.
    _with_widgets(monkeypatch, [_dialog("PFC2D 7.00", "Ok")])

    result = autostart.answer_dialog(1, "Ok", timeout=0.2)

    assert result["status"] == "error"
    assert "not running in this process" in result["message"]
    assert autostart._pending_answer is None


def test_two_answers_at_once_are_refused_rather_than_raced(monkeypatch):
    _with_widgets(monkeypatch, [_dialog("PFC2D 7.00", "Ok")])
    monkeypatch.setattr(autostart, "_pending_answer", (99, 1, "ok"))

    result = autostart.answer_dialog(1, "Ok", timeout=0.2)

    assert result["status"] == "error"
    assert "already in flight" in result["message"]
    # And the one that was already posted is left where it was.
    assert autostart._pending_answer == (99, 1, "ok")


def test_the_label_matches_however_the_client_cases_it(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    box = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [box])
    autostart.refresh_dialogs()

    result, _ = _post_and_drain(1, "  oK  ")

    assert result["status"] == "success"
    assert box.buttons[0].clicks == 1


def test_an_answered_dialog_is_not_swept_again_by_the_policy(monkeypatch, tmp_path):
    # The client's answer lands first; the automatic pass then finds the box
    # already accounted for and does not click a second time.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    box = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [box])
    autostart.refresh_dialogs()

    _post_and_drain(1, "Ok")
    autostart._tick_windows()
    autostart._tick_windows()

    assert box.buttons[0].clicks == 1
    assert "dismissed a dialog that asks nothing" not in _log_of(tmp_path)


# ---- installing the shim ----------------------------------------------


def test_shim_carries_the_marker_and_imports_the_module():
    assert autostart.MARKER in autostart.SHIM
    assert "from itasca_mcp_bridge.autostart import boot" in autostart.SHIM
    # A shim, not a copy: the logic has to keep upgrading with the package.
    assert "def _watch" not in autostart.SHIM


def test_install_then_status_then_remove(tmp_path):
    site_packages = str(tmp_path / "Lib" / "site-packages")
    os.makedirs(site_packages)
    path = autostart.target_of(site_packages)

    assert autostart.state_of(path) == "absent"
    assert autostart.install(site_packages) == "installed"
    assert autostart.state_of(path) == "ours"
    assert autostart.install(site_packages) == "refreshed"
    assert autostart.remove(site_packages) == "removed"
    assert autostart.state_of(path) == "absent"
    assert autostart.remove(site_packages) == "nothing to remove"


def test_a_foreign_sitecustomize_is_backed_up_not_lost(tmp_path):
    site_packages = str(tmp_path / "Lib" / "site-packages")
    os.makedirs(site_packages)
    path = autostart.target_of(site_packages)
    original = "# somebody else's sitecustomize\nX = 1\n"
    with open(path, "w") as handle:
        handle.write(original)

    assert autostart.state_of(path) == "foreign"
    result = autostart.install(site_packages)
    assert result.startswith("replaced (backup at ")
    assert autostart.state_of(path) == "ours"

    # And removing ours puts theirs back, rather than leaving them without one.
    assert autostart.remove(site_packages) == "removed (restored the previous file)"
    with open(path) as handle:
        assert handle.read() == original


def test_remove_leaves_a_foreign_file_alone(tmp_path):
    site_packages = str(tmp_path / "Lib" / "site-packages")
    os.makedirs(site_packages)
    path = autostart.target_of(site_packages)
    with open(path, "w") as handle:
        handle.write("SOMEONE_ELSES = True\n")

    assert autostart.remove(site_packages) == "left alone (not this package's file)"
    assert os.path.exists(path)


# ---- finding the products ---------------------------------------------


def test_products_are_found_once_each(tmp_path):
    # Both spellings are probed because the case of that directory is not
    # guaranteed, and on Windows they are the same directory. Without the
    # de-duplication every product is reported -- and installed to -- twice.
    python36 = tmp_path / "PFC700" / "exe64" / "python36"
    os.makedirs(str(python36 / "Lib" / "site-packages"))
    try:
        (python36 / "lib").symlink_to(python36 / "Lib", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot link lib -> Lib here to exercise the aliasing")
    # A product whose embedded Python is not there yet.
    os.makedirs(str(tmp_path / "FLAC3D700" / "exe64"))

    found = autostart.product_python_dirs([str(tmp_path)])
    assert [product for product, _ in found] == ["PFC700"]
