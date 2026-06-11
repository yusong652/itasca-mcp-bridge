"""Tests for post-upgrade release announcements."""

from __future__ import annotations

from itasca_mcp_bridge import announce


FAKE_ANNOUNCEMENTS = {
    "0.2.0": ("auto upgrade",),
    "0.2.10": ("ten things", "and one more"),
    "0.2.2": ("small win",),
    "not-a-version": ("never shown",),
}


class TestCollect:
    def test_range_is_exclusive_inclusive(self, monkeypatch):
        monkeypatch.setattr(announce, "ANNOUNCEMENTS", FAKE_ANNOUNCEMENTS)
        entries = announce._collect(since="0.2.0", until="0.2.10")
        assert [version for version, _notes in entries] == ["0.2.2", "0.2.10"]

    def test_sorted_numerically_not_lexically(self, monkeypatch):
        monkeypatch.setattr(announce, "ANNOUNCEMENTS", FAKE_ANNOUNCEMENTS)
        entries = announce._collect()
        assert [version for version, _notes in entries] == ["0.2.0", "0.2.2", "0.2.10"]

    def test_unparsable_keys_skipped(self, monkeypatch):
        monkeypatch.setattr(announce, "ANNOUNCEMENTS", FAKE_ANNOUNCEMENTS)
        versions = [version for version, _notes in announce._collect()]
        assert "not-a-version" not in versions

    def test_unparsable_boundary_ignored(self, monkeypatch):
        monkeypatch.setattr(announce, "ANNOUNCEMENTS", FAKE_ANNOUNCEMENTS)
        entries = announce._collect(since="unknown", until="0.2.2")
        assert [version for version, _notes in entries] == ["0.2.0", "0.2.2"]

    def test_no_match_returns_empty(self, monkeypatch):
        monkeypatch.setattr(announce, "ANNOUNCEMENTS", FAKE_ANNOUNCEMENTS)
        assert announce._collect(since="0.2.10") == []


class TestWhatsNew:
    def test_prints_versions_and_notes(self, monkeypatch, capsys):
        monkeypatch.setattr(announce, "ANNOUNCEMENTS", FAKE_ANNOUNCEMENTS)
        announce.whats_new(since="0.2.2", until="0.2.10")
        out = capsys.readouterr().out
        assert "What's new:" in out
        assert "0.2.10:" in out
        assert "- ten things" in out
        assert "- and one more" in out
        assert "0.2.0" not in out

    def test_silent_when_nothing_to_announce(self, monkeypatch, capsys):
        monkeypatch.setattr(announce, "ANNOUNCEMENTS", FAKE_ANNOUNCEMENTS)
        announce.whats_new(since="0.2.10")
        assert capsys.readouterr().out == ""

    def test_exported_from_package(self):
        import itasca_mcp_bridge

        assert itasca_mcp_bridge.whats_new is announce.whats_new
