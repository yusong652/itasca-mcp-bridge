# -*- coding: utf-8 -*-
"""Best-effort self-upgrade for the bridge package.

`start()` calls into this module before doing anything stateful so that
users who launch the bridge with the plain two-liner::

    import itasca_mcp_bridge
    itasca_mcp_bridge.start()

still pick up new releases without running a separate bootstrap script.

Upgrading is strictly best-effort: a version check with a short hard
timeout decides whether pip runs at all, and any failure on the upgrade
path (offline machine, blocked proxy, pip error) falls back to starting
the already-installed version. The in-process pip call itself cannot be
timed out, which is exactly why the cheap pre-check exists.

Must stay compatible with Python 3.6 (PFC 6/7 embedded interpreter).
"""

import importlib
import json
import logging
import os
import re
import sys

PACKAGE_NAME = "itasca-mcp-bridge"
MODULE_NAME = "itasca_mcp_bridge"

# Set to "0" (or "false"/"no"/"off") to skip the update check entirely.
# addon.py-style bootstrap scripts set this before calling start() so the
# upgrade they already performed is not re-checked.
ENV_AUTO_UPGRADE = "ITASCA_MCP_BRIDGE_AUTO_UPGRADE"

# Custom package index (corporate mirrors). The legacy PFC-specific name
# is honoured for users who configured it for addon.py.
ENV_INDEX_URL = "ITASCA_MCP_PIP_INDEX_URL"
ENV_INDEX_URL_LEGACY = "PFC_MCP_PIP_INDEX_URL"

# Internal handoff: set just before the freshly upgraded module's start()
# runs, so its startup banner can report the version jump. Popped on read.
ENV_UPGRADED_FROM = "_ITASCA_MCP_BRIDGE_UPGRADED_FROM"

VERSION_CHECK_TIMEOUT_S = 5

# Index URLs tried in order. Mirrors act as a fallback when the primary
# is unreachable (corporate proxies, slow international routes).
DEFAULT_INDEXES = [
    ("https://pypi.org/simple/", ("pypi.org", "files.pythonhosted.org")),
    ("https://pypi.tuna.tsinghua.edu.cn/simple/", ("pypi.tuna.tsinghua.edu.cn",)),
]

PYPI_JSON_URL = "https://pypi.org/pypi/{}/json".format(PACKAGE_NAME)

_FILENAME_VERSION_RE = re.compile(
    r"itasca[-_]mcp[-_]bridge-(\d+(?:\.\d+)+)", re.IGNORECASE
)


def env_allows_upgrade():
    # type: () -> bool
    value = os.environ.get(ENV_AUTO_UPGRADE, "")
    return value.strip().lower() not in ("0", "false", "no", "off")


def _index_override():
    # type: () -> str
    return os.environ.get(ENV_INDEX_URL) or os.environ.get(ENV_INDEX_URL_LEGACY) or ""


def _parse_version(text):
    # type: (str) -> tuple
    """Parse 'X.Y.Z' into an int tuple; None if not plain dotted ints."""
    try:
        return tuple(int(part) for part in text.strip().split("."))
    except (AttributeError, ValueError):
        return None


def _fetch_url(url):
    # type: (str) -> str
    """GET a URL with a hard timeout; None on any failure."""
    try:
        from urllib.request import urlopen

        response = urlopen(url, timeout=VERSION_CHECK_TIMEOUT_S)
        try:
            return response.read().decode("utf-8", "replace")
        finally:
            response.close()
    except Exception:
        return None


def _latest_from_pypi_json():
    # type: () -> str
    body = _fetch_url(PYPI_JSON_URL)
    if body is None:
        return None
    try:
        return json.loads(body)["info"]["version"]
    except Exception:
        return None


def _latest_from_simple_index(index_url):
    # type: (str) -> str
    """Scrape the newest version from a PEP 503 simple-index project page."""
    page_url = index_url.rstrip("/") + "/" + PACKAGE_NAME + "/"
    body = _fetch_url(page_url)
    if body is None:
        return None
    versions = []
    for match in _FILENAME_VERSION_RE.findall(body):
        parsed = _parse_version(match)
        if parsed is not None:
            versions.append((parsed, match))
    if not versions:
        return None
    return max(versions)[1]


def check_latest_version():
    # type: () -> str
    """Best-effort latest published version; None if unreachable."""
    override = _index_override()
    if override:
        return _latest_from_simple_index(override)

    latest = _latest_from_pypi_json()
    if latest is not None:
        return latest
    # PyPI unreachable; the mirror serves the simple index only.
    for index_url, _hosts in DEFAULT_INDEXES[1:]:
        latest = _latest_from_simple_index(index_url)
        if latest is not None:
            return latest
    return None


def _resolve_pip_main():
    """Locate pip's callable entry point.

    There is no single stable location. `pip.main` exists in pip <= 9
    (what PFC 6.0 ships), was removed in pip 10.0, and was later restored
    as an internal-only shim; `pip._internal.main` covers pip 10 .. 19.2;
    `pip._internal.cli.main.main` covers pip >= 19.3. The embedded PFC
    Python may carry any pip version, so probe each location in turn
    rather than guessing from the pip or Python version.
    """
    try:
        from pip._internal.cli.main import main as pip_main  # pip >= 19.3

        return pip_main
    except Exception:
        pass
    try:
        from pip._internal import main as pip_main  # pip 10 .. 19.2

        return pip_main
    except Exception:
        pass
    try:
        from pip import main as pip_main  # pip <= 9 (PFC 6.0)

        return pip_main
    except Exception:
        pass
    return None


def _build_install_args(index_url, trusted_hosts):
    args = [
        "install",
        "--user",
        "-U",
        "--disable-pip-version-check",
        "--default-timeout", "120",
        "--retries", "2",
        "--index-url", index_url,
    ]
    for host in trusted_hosts:
        args += ["--trusted-host", host]
    if sys.version_info >= (3, 10):
        args += ["--no-warn-script-location", "--progress-bar", "off"]
    args.append(PACKAGE_NAME)
    return args


def _run_pip(args):
    pip_main = _resolve_pip_main()
    if pip_main is None:
        return 1

    # The product GUI hosts pip inside an IPython process; temporarily
    # suppress logging handler tracebacks that don't reflect actual
    # installation failures.
    previous_raise_exceptions = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        return pip_main(list(args))
    finally:
        logging.raiseExceptions = previous_raise_exceptions


def _install_latest():
    # type: () -> bool
    os.environ["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    override = _index_override()
    if override:
        indexes = [(override, ())]
    else:
        indexes = DEFAULT_INDEXES

    for attempt, (index_url, trusted_hosts) in enumerate(indexes, start=1):
        if attempt > 1:
            print("Primary index failed, retrying with mirror: {}".format(index_url))
        if _run_pip(_build_install_args(index_url, trusted_hosts)) == 0:
            return True
    return False


def maybe_upgrade(current_version):
    # type: (str) -> bool
    """Check for and install a newer bridge release. True if pip installed one.

    Never raises in normal operation; every failure path degrades to
    "keep the installed version".
    """
    latest = check_latest_version()
    if latest is None:
        print(
            "{}: update check skipped (index unreachable); "
            "starting installed version {}.".format(PACKAGE_NAME, current_version)
        )
        return False

    current_parsed = _parse_version(current_version)
    latest_parsed = _parse_version(latest)
    if current_parsed is None or latest_parsed is None or latest_parsed <= current_parsed:
        return False

    print(
        "{} {} is available (installed: {}). Upgrading ...".format(
            PACKAGE_NAME, latest, current_version
        )
    )
    if not _install_latest():
        print(
            "{}: upgrade failed; starting installed version {}. "
            "The pip error is in the output above. To upgrade manually:\n"
            "    python -m pip install --user -U {}".format(
                PACKAGE_NAME, current_version, PACKAGE_NAME
            )
        )
        return False
    return True


def _ensure_user_site_on_path():
    try:
        import site

        user_site = site.getusersitepackages()
    except Exception:
        return

    if isinstance(user_site, str) and user_site and user_site not in sys.path:
        sys.path.append(user_site)


def reload_bridge():
    """Drop the loaded bridge (and websockets) modules and re-import fresh.

    Called after pip replaced the package on disk so the new code is what
    actually runs. Safe at this point because start() performs the upgrade
    before creating any state (no logging config, callbacks, or server yet).
    The caller's frame keeps the old module objects alive until it returns.
    """
    _ensure_user_site_on_path()
    for name in list(sys.modules):
        if name == MODULE_NAME or name.startswith(MODULE_NAME + "."):
            del sys.modules[name]
        elif name == "websockets" or name.startswith("websockets."):
            del sys.modules[name]
    importlib.invalidate_caches()
    return importlib.import_module(MODULE_NAME)
