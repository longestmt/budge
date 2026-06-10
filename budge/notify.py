"""Failure notification to OpenClaw (PRD section 7.7).

HARD BOUNDARY, enforced by what this module simply does not contain:
OpenClaw's role is read-only triage and notification. This code only ever
SENDS data outward (unit name, exit status, log tail, pending count). There
is no code path here — or anywhere in budge — by which OpenClaw can write to
the journal, the rules, secrets, or git.
"""

from __future__ import annotations

import json
import socket
import urllib.request

from . import journal
from .util import dry, say, warn


def _post(url: str, payload: dict) -> None:
    if not url:
        warn("notify.openclaw_url not configured; printing instead:")
        say(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if dry(f"POST notification to {url}"):
        return
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception as e:
        # Never let the notifier turn one failure into two; log and exit 0.
        warn(f"could not reach OpenClaw: {e}")


def notify_failure(cfg, unit: str) -> None:
    """OnFailure= hook: unit name, exit status, last ~20 journalctl lines."""
    from .util import run

    status = ""
    proc = run(["systemctl", "show", "-p", "Result,ExecMainStatus", unit],
               check=False)
    if proc.returncode == 0:
        status = proc.stdout.strip().replace("\n", ", ")
    proc = run(["journalctl", "-u", unit, "-n", "20", "--no-pager"],
               check=False)
    log_tail = proc.stdout.strip() if proc.returncode == 0 \
        else "(journalctl unavailable)"
    _post(cfg.openclaw_url, {
        "type": "budge.failure",
        "host": socket.gethostname(),
        "unit": unit,
        "status": status,
        "log_tail": log_tail,
        "note": ("If this is budge-fetch, a failed balance assertion is "
                 "often a bank-pending transaction and may clear by the "
                 "next run."),
    })


def notify_review_ready(cfg) -> None:
    """Weekly nudge: 'review ready — N transactions pending'."""
    entries = journal.parse_pending(cfg.repo / "pending.journal")
    n = len(entries)
    if n == 0:
        say("nothing pending; no nudge sent")
        return
    _post(cfg.openclaw_url, {
        "type": "budge.review_ready",
        "host": socket.gethostname(),
        "message": f"review ready — {n} transactions pending",
        "pending": n,
    })
