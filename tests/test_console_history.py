"""ConsoleHistory: append, deliver once, survive a restart."""

from __future__ import annotations

import json

import pytest

from itasca_mcp_bridge.console.history import (
    CURSOR_FILENAME,
    HISTORY_FILENAME,
    SOURCE_COMMAND,
    SOURCE_PYTHON,
    ConsoleHistory,
)


@pytest.fixture
def history(tmp_path):
    return ConsoleHistory(directory=str(tmp_path))


def test_add_assigns_ids_and_records_fields(history):
    entry = history.add(SOURCE_PYTHON, "1+1", output="", result=2, success=True)
    assert entry["id"] == 1
    assert entry["source"] == "python"
    assert entry["input"] == "1+1"
    assert entry["result"] == 2
    assert entry["success"] is True
    assert entry["timestamp"] > 0

    second = history.add(SOURCE_COMMAND, "fish list", output="Name Type Value")
    assert second["id"] == 2
    assert second["source"] == "command"
    assert second["result"] is None


def test_consume_delivers_each_entry_once(history):
    history.add(SOURCE_PYTHON, "a")
    history.add(SOURCE_COMMAND, "b")

    first = history.consume()
    assert [e["input"] for e in first["entries"]] == ["a", "b"]
    assert first["cursor"] == 2
    assert first["has_more"] is False

    again = history.consume()
    assert again["entries"] == []
    assert again["cursor"] == 2

    history.add(SOURCE_PYTHON, "c")
    third = history.consume()
    assert [e["input"] for e in third["entries"]] == ["c"]


def test_consume_limit_keeps_the_newest_and_skips_the_rest(history):
    for i in range(5):
        history.add(SOURCE_PYTHON, "cell {}".format(i))

    result = history.consume(limit=2)
    assert [e["input"] for e in result["entries"]] == ["cell 3", "cell 4"]
    # The cursor moved past everything: the older three are not replayed.
    assert result["cursor"] == 5
    assert result["has_more"] is False
    assert history.consume()["entries"] == []


def test_consume_tolerates_bad_limit(history):
    history.add(SOURCE_PYTHON, "x")
    assert len(history.consume(limit="nonsense")["entries"]) == 1
    history.add(SOURCE_PYTHON, "y")
    assert len(history.consume(limit=0)["entries"]) == 1


def test_persists_entries_and_cursor_across_restart(tmp_path):
    first = ConsoleHistory(directory=str(tmp_path))
    first.add(SOURCE_PYTHON, "seen")
    first.consume()
    first.add(SOURCE_COMMAND, "unseen")

    assert (tmp_path / HISTORY_FILENAME).exists()
    assert (tmp_path / CURSOR_FILENAME).exists()

    second = ConsoleHistory(directory=str(tmp_path))
    assert len(second) == 2
    result = second.consume()
    assert [e["input"] for e in result["entries"]] == ["unseen"]
    # Ids keep counting from the persisted maximum.
    assert second.add(SOURCE_PYTHON, "next")["id"] == 3


def test_load_skips_torn_and_foreign_lines(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    good = json.dumps({"id": 7, "source": "python", "input": "ok", "output": "", "result": None,
                       "success": True, "timestamp": 1.0})
    path.write_text(good + "\n" + '{"id": 8, "input": "torn' + "\n" + "not json\n" + '["list"]\n',
                    encoding="utf-8")

    history = ConsoleHistory(directory=str(tmp_path))
    assert len(history) == 1
    assert history.add(SOURCE_PYTHON, "after")["id"] == 8


def test_prunes_to_max_entries_and_rewrites_file(tmp_path):
    history = ConsoleHistory(max_entries=3, directory=str(tmp_path))
    for i in range(5):
        history.add(SOURCE_PYTHON, "cell {}".format(i))

    assert len(history) == 3
    lines = (tmp_path / HISTORY_FILENAME).read_text(encoding="utf-8").strip().split("\n")
    assert [json.loads(line)["input"] for line in lines] == ["cell 2", "cell 3", "cell 4"]


def test_on_new_entry_callback_runs_and_cannot_break_add(history):
    seen = []
    history.on_new_entry = lambda entry: seen.append(entry["id"])
    history.add(SOURCE_PYTHON, "a")
    assert seen == [1]

    def boom(entry):
        raise RuntimeError("doorbell broke")

    history.on_new_entry = boom
    assert history.add(SOURCE_PYTHON, "b")["id"] == 2


def test_result_serialization(history):
    class Odd:
        def __repr__(self):
            return "<Odd>"

    assert history.add(SOURCE_PYTHON, "a", result=Odd())["result"] == "<Odd>"
    assert history.add(SOURCE_PYTHON, "b", result=[1, 2])["result"] == "[1, 2]"
    assert history.add(SOURCE_PYTHON, "c", result="s")["result"] == "s"
    assert history.add(SOURCE_PYTHON, "d", result=None)["result"] is None
