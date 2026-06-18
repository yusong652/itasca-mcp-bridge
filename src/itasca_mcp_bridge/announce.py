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
