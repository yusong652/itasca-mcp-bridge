# -*- coding: utf-8 -*-
"""Per-version release highlights, shown to users right after a self-upgrade.

The announcements ship inside the package itself: after `start()`
self-upgrades and reloads, the freshly installed code knows exactly what
changed, so no network call or changelog fetch is needed. The startup
banner hook prints every entry in the `(old version, new version]` jump.

Maintaining `ANNOUNCEMENTS`:
- Add a key only for releases with user-visible improvements; silent
  fixes don't need one.
- Keep notes to one or two short lines each -- this prints in the ITASCA
  console on someone else's screen.
- Entries for old versions may be pruned; they only ever display for
  users jumping from a version older than them.

Must stay compatible with Python 3.6 (PFC 6/7 embedded interpreter).
"""

from .upgrade import _parse_version

ANNOUNCEMENTS = {
    # The (since, until] display window means a key can only ever show for
    # users upgrading from an older release through the self-upgrade path,
    # which first shipped in 0.2.0 -- so 0.2.1 is the earliest key that can
    # display, and it carries the 0.2.0 highlights too.
    "0.2.1": (
        "start() now keeps the bridge up to date: it checks PyPI for a newer "
        "release (Tsinghua mirror fallback) and self-upgrades before starting. "
        "Pin with start(auto_upgrade=False) or ITASCA_MCP_BRIDGE_AUTO_UPGRADE=0.",
        "After a self-upgrade, this whats-new list shows what the new release "
        "brings; reprint it anytime with itasca_mcp_bridge.whats_new().",
    ),
    "0.4.0": (
        "Transport switched from WebSocket to stdlib HTTP + SSE: the bridge is "
        "now HTTP-only and stays dependency-free (any embedded Python 3.6+).",
        "Your MCP client must speak HTTP+SSE -- use itasca-mcp >= 0.6.0. "
        "pfc-mcp / flac-mcp users: migrate to itasca-mcp (WebSocket clients can't connect).",
    ),
    "0.4.3": (
        "Self-upgrade fixed for consoles whose stdout lacks isatty: pip's "
        "progress bar crashed the download, so upgrades silently never landed "
        "(stock PFC 6/7 pip was affected).",
        "The upgrade-failed hint now prints the product Python's full path -- "
        "a plain 'python' would install into the system interpreter instead.",
    ),
    "0.4.4": (
        "Multi-line itasca.command() batches are now safe end-to-end: FISH "
        "define blocks survive splitting, and a mid-batch 'model new' no "
        "longer leaves the bridge unreachable or tasks uninterruptible.",
        "PFC 6: interrupting a task now reports 'interrupted' (was 'failed'), "
        "and a live execute_code during cycling no longer silently stops the run.",
    ),
    "0.4.5": (
        "Fixed the bridge going silent when the agent cycles after a 'model new' "
        "or 'model restore' issued in the GUI console, File menu, or a called "
        ".dat: the bridge now repairs its cycle callbacks before every execution.",
        "Cycling inside a called .dat that itself contains 'model new' still "
        "blocks until the file returns -- run such files as inline commands.",
    ),
    "0.5.0": (
        "program call '<file>' now runs the data file inline, one command per "
        "engine call: a called .dat with 'model new' keeps the bridge reachable, "
        "stays interruptible, and its console output lands in the task log as "
        "it happens (nested calls, program return, label/line honored).",
    ),
}


def _collect(since=None, until=None):
    """Announcements for versions in (since, until], oldest first.

    An unparsable or missing boundary is ignored (no bound on that side).
    Returns a list of (version, notes) tuples.
    """
    since_parsed = _parse_version(since) if since else None
    until_parsed = _parse_version(until) if until else None

    entries = []
    for version, notes in ANNOUNCEMENTS.items():
        parsed = _parse_version(version)
        if parsed is None:
            continue
        if since_parsed is not None and parsed <= since_parsed:
            continue
        if until_parsed is not None and parsed > until_parsed:
            continue
        entries.append((parsed, version, tuple(notes)))
    entries.sort()
    return [(version, notes) for _parsed, version, notes in entries]


def whats_new(since=None, until=None):
    """Print release highlights for versions in (since, until].

    With no arguments, prints every announcement the installed package
    carries. Call it from the product console as
    `itasca_mcp_bridge.whats_new()`.
    """
    entries = _collect(since, until)
    if not entries:
        return
    print("What's new:")
    for version, notes in entries:
        print("  {}:".format(version))
        for note in notes:
            print("    - {}".format(note))
    print("")
