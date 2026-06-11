"""Tests for the best-effort self-upgrade path.

Network and pip are always mocked; these tests pin down the decision
logic (when to upgrade, when to fall back to the installed version) and
the index-page version scraping.
"""

from __future__ import annotations

import sys

from itasca_mcp_bridge import upgrade


SIMPLE_INDEX_HTML = """
<!DOCTYPE html>
<html><body>
<a href="../../packages/.../itasca_mcp_bridge-0.1.4-py2.py3-none-any.whl">itasca_mcp_bridge-0.1.4-py2.py3-none-any.whl</a>
<a href="../../packages/.../itasca_mcp_bridge-0.1.10-py2.py3-none-any.whl">itasca_mcp_bridge-0.1.10-py2.py3-none-any.whl</a>
<a href="../../packages/.../itasca-mcp-bridge-0.1.2.tar.gz">itasca-mcp-bridge-0.1.2.tar.gz</a>
</body></html>
"""


class TestParseVersion:
    def test_dotted_ints(self):
        assert upgrade._parse_version("0.1.6") == (0, 1, 6)

    def test_orders_numerically_not_lexically(self):
        assert upgrade._parse_version("0.1.10") > upgrade._parse_version("0.1.9")

    def test_rejects_non_numeric(self):
        assert upgrade._parse_version("0.2.0rc1") is None
        assert upgrade._parse_version("") is None
        assert upgrade._parse_version(None) is None


class TestEnvAllowsUpgrade:
    def test_default_allows(self, monkeypatch):
        monkeypatch.delenv(upgrade.ENV_AUTO_UPGRADE, raising=False)
        assert upgrade.env_allows_upgrade()

    def test_disabled_values(self, monkeypatch):
        for value in ("0", "false", "False", "NO", "off"):
            monkeypatch.setenv(upgrade.ENV_AUTO_UPGRADE, value)
            assert not upgrade.env_allows_upgrade()

    def test_other_values_allow(self, monkeypatch):
        monkeypatch.setenv(upgrade.ENV_AUTO_UPGRADE, "1")
        assert upgrade.env_allows_upgrade()


class TestLatestFromSimpleIndex:
    def test_picks_numerically_newest(self, monkeypatch):
        monkeypatch.setattr(upgrade, "_fetch_url", lambda url: SIMPLE_INDEX_HTML)
        assert upgrade._latest_from_simple_index("https://mirror/simple/") == "0.1.10"

    def test_unreachable_returns_none(self, monkeypatch):
        monkeypatch.setattr(upgrade, "_fetch_url", lambda url: None)
        assert upgrade._latest_from_simple_index("https://mirror/simple/") is None

    def test_no_matching_files_returns_none(self, monkeypatch):
        monkeypatch.setattr(upgrade, "_fetch_url", lambda url: "<html></html>")
        assert upgrade._latest_from_simple_index("https://mirror/simple/") is None


class TestCheckLatestVersion:
    def test_prefers_primary_simple_index(self, monkeypatch):
        # The simple HTML page refreshes before the JSON API after a release;
        # the JSON API must not even be queried when the page answers.
        monkeypatch.setattr(upgrade, "_latest_from_simple_index", lambda url: "0.2.0")
        monkeypatch.setattr(
            upgrade, "_latest_from_pypi_json",
            lambda: (_ for _ in ()).throw(AssertionError("JSON API is the fallback")),
        )
        assert upgrade.check_latest_version() == "0.2.0"

    def test_falls_back_to_json_api(self, monkeypatch):
        monkeypatch.setattr(upgrade, "_latest_from_simple_index", lambda url: None)
        monkeypatch.setattr(upgrade, "_latest_from_pypi_json", lambda: "0.2.0")
        assert upgrade.check_latest_version() == "0.2.0"

    def test_falls_back_to_mirror_last(self, monkeypatch):
        monkeypatch.setattr(upgrade, "_latest_from_pypi_json", lambda: None)
        seen = []

        def fake_simple(index_url):
            seen.append(index_url)
            if index_url == upgrade.DEFAULT_INDEXES[1][0]:
                return "0.1.9"
            return None

        monkeypatch.setattr(upgrade, "_latest_from_simple_index", fake_simple)
        assert upgrade.check_latest_version() == "0.1.9"
        assert seen == [upgrade.DEFAULT_INDEXES[0][0], upgrade.DEFAULT_INDEXES[1][0]]

    def test_index_override_skips_pypi_json(self, monkeypatch):
        monkeypatch.setenv(upgrade.ENV_INDEX_URL, "https://corp/simple/")
        monkeypatch.setattr(
            upgrade, "_latest_from_pypi_json",
            lambda: (_ for _ in ()).throw(AssertionError("must not query pypi.org")),
        )
        monkeypatch.setattr(upgrade, "_latest_from_simple_index", lambda url: "0.3.0")
        assert upgrade.check_latest_version() == "0.3.0"

    def test_legacy_override_honoured(self, monkeypatch):
        monkeypatch.delenv(upgrade.ENV_INDEX_URL, raising=False)
        monkeypatch.setenv(upgrade.ENV_INDEX_URL_LEGACY, "https://corp/simple/")
        seen = []

        def fake_simple(index_url):
            seen.append(index_url)
            return "0.3.0"

        monkeypatch.setattr(upgrade, "_latest_from_simple_index", fake_simple)
        assert upgrade.check_latest_version() == "0.3.0"
        assert seen == ["https://corp/simple/"]


class TestMaybeUpgrade:
    def test_index_unreachable_skips(self, monkeypatch):
        monkeypatch.setattr(upgrade, "check_latest_version", lambda: None)
        monkeypatch.setattr(
            upgrade, "_install_latest",
            lambda: (_ for _ in ()).throw(AssertionError("pip must not run")),
        )
        assert upgrade.maybe_upgrade("0.1.6") is False

    def test_already_latest_skips(self, monkeypatch):
        monkeypatch.setattr(upgrade, "check_latest_version", lambda: "0.1.6")
        assert upgrade.maybe_upgrade("0.1.6") is False

    def test_older_published_skips(self, monkeypatch):
        monkeypatch.setattr(upgrade, "check_latest_version", lambda: "0.1.5")
        assert upgrade.maybe_upgrade("0.1.6") is False

    def test_unparsable_versions_skip(self, monkeypatch):
        monkeypatch.setattr(upgrade, "check_latest_version", lambda: "0.2.0rc1")
        assert upgrade.maybe_upgrade("0.1.6") is False

    def test_newer_triggers_install(self, monkeypatch):
        monkeypatch.setattr(upgrade, "check_latest_version", lambda: "0.2.0")
        monkeypatch.setattr(upgrade, "_install_latest", lambda: True)
        assert upgrade.maybe_upgrade("0.1.6") is True

    def test_install_failure_falls_back(self, monkeypatch):
        monkeypatch.setattr(upgrade, "check_latest_version", lambda: "0.2.0")
        monkeypatch.setattr(upgrade, "_install_latest", lambda: False)
        assert upgrade.maybe_upgrade("0.1.6") is False


class TestInstallLatest:
    def test_tries_mirror_after_primary_failure(self, monkeypatch):
        monkeypatch.delenv(upgrade.ENV_INDEX_URL, raising=False)
        monkeypatch.delenv(upgrade.ENV_INDEX_URL_LEGACY, raising=False)
        calls = []

        def fake_run_pip(args):
            calls.append(args)
            return 1 if len(calls) == 1 else 0

        monkeypatch.setattr(upgrade, "_run_pip", fake_run_pip)
        assert upgrade._install_latest() is True
        assert len(calls) == 2
        assert DEFAULT_PRIMARY in calls[0]
        assert DEFAULT_MIRROR in calls[1]

    def test_override_replaces_default_indexes(self, monkeypatch):
        monkeypatch.setenv(upgrade.ENV_INDEX_URL, "https://corp/simple/")
        calls = []

        def fake_run_pip(args):
            calls.append(args)
            return 1

        monkeypatch.setattr(upgrade, "_run_pip", fake_run_pip)
        assert upgrade._install_latest() is False
        assert len(calls) == 1
        assert "https://corp/simple/" in calls[0]


DEFAULT_PRIMARY = upgrade.DEFAULT_INDEXES[0][0]
DEFAULT_MIRROR = upgrade.DEFAULT_INDEXES[1][0]


class TestStartDelegation:
    def test_upgrade_delegates_to_fresh_start(self, monkeypatch):
        import os

        import itasca_mcp_bridge

        monkeypatch.delenv(upgrade.ENV_AUTO_UPGRADE, raising=False)
        monkeypatch.setattr(upgrade, "maybe_upgrade", lambda current: True)

        recorded = {}

        class FreshModule:
            __version__ = "9.9.9"

            @staticmethod
            def start(**kwargs):
                recorded.update(kwargs)
                recorded["upgraded_from"] = os.environ.get(upgrade.ENV_UPGRADED_FROM)
                return "fresh-started"

        monkeypatch.setattr(upgrade, "reload_bridge", lambda: FreshModule)

        try:
            result = itasca_mcp_bridge.start(port=1234, mode="console")
        finally:
            os.environ.pop(upgrade.ENV_UPGRADED_FROM, None)

        assert result == "fresh-started"
        assert recorded["port"] == 1234
        assert recorded["mode"] == "console"
        assert recorded["auto_upgrade"] is False
        assert recorded["upgraded_from"] == itasca_mcp_bridge.__version__

    def test_env_disable_skips_check_entirely(self, monkeypatch):
        import itasca_mcp_bridge
        from itasca_mcp_bridge import runtime

        monkeypatch.setenv(upgrade.ENV_AUTO_UPGRADE, "0")
        monkeypatch.setattr(
            upgrade, "maybe_upgrade",
            lambda current: (_ for _ in ()).throw(AssertionError("must not check")),
        )
        monkeypatch.setattr(runtime, "start", lambda **kwargs: "runtime-started")

        assert itasca_mcp_bridge.start() == "runtime-started"


class TestReloadBridge:
    def test_returns_fresh_module(self):
        import itasca_mcp_bridge

        old_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "itasca_mcp_bridge" or name.startswith("itasca_mcp_bridge.")
        }
        try:
            fresh = upgrade.reload_bridge()
            assert fresh is not itasca_mcp_bridge
            assert fresh.__name__ == "itasca_mcp_bridge"
            assert hasattr(fresh, "start")
        finally:
            # Restore the original module objects so other tests keep
            # operating on the instances they imported at collection time.
            for name in list(sys.modules):
                if name == "itasca_mcp_bridge" or name.startswith("itasca_mcp_bridge."):
                    del sys.modules[name]
            sys.modules.update(old_modules)
