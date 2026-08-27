"""budge — glue scripts around a stock hledger / SimpleFIN / Paisa stack.

Prime directive: no frankensteining. Everything in this package talks to the
stock components only through their stable interfaces (SimpleFIN API, CSV,
hledger journal format, hledger CLI). Each module is individually replaceable
without any change to the data.
"""

from __future__ import annotations

import json
from importlib import metadata

__version__ = "1.2.2"


def installed_commit() -> str:
    """Return the VCS commit recorded by pip, when installed from Git."""
    try:
        direct_url = metadata.distribution("budge").read_text(
            "direct_url.json")
        data = json.loads(direct_url or "{}")
        return data.get("vcs_info", {}).get("commit_id", "")
    except (metadata.PackageNotFoundError, json.JSONDecodeError, OSError):
        return ""


def version_string() -> str:
    commit = installed_commit()
    suffix = f" ({commit[:12]})" if commit else ""
    return f"budge {__version__}{suffix}"
