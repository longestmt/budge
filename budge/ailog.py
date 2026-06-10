"""ai/ai-decisions.log — append-only audit log (PRD section 7.6 via 7.4).

JSON-lines, one event per line, never rewritten. The PRD asks that the
accepted/overridden field be "updated at promote" while also requiring the log
to be append-only; budge resolves that tension event-sourced: promote APPENDS
an `outcome` event per transaction rather than editing history. The latest
event for a txn_id is its current state.

Events:
  suggest          AI proposed a category (before it is used — PRD section 8)
  reject           AI output was malformed; txn left uncategorized
  manual_override  operator one-off correction in review (survives regeneration)
  outcome          promote result: accepted | overridden
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .util import dry


def log_path(repo: Path) -> Path:
    return Path(repo) / "ai" / "ai-decisions.log"


def append(repo: Path, **record) -> None:
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    line = json.dumps(record, ensure_ascii=False)
    if dry(f"append to ai-decisions.log: {line}"):
        return
    path = log_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_all(repo: Path) -> list:
    path = log_path(repo)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # never let a bad line break the pipeline
    return events


def latest_suggestions(repo: Path) -> dict:
    """txn_id -> most recent suggest event."""
    out = {}
    for ev in read_all(repo):
        if ev.get("event") == "suggest":
            out[ev.get("txn_id")] = ev
    return out


def manual_overrides(repo: Path) -> dict:
    """txn_id -> category chosen by the operator (one-off corrections)."""
    out = {}
    for ev in read_all(repo):
        if ev.get("event") == "manual_override":
            out[ev.get("txn_id")] = ev.get("category")
    return out


def recent_accepted_examples(repo: Path, limit: int = 15) -> list:
    """Recently accepted (payee -> category) pairs, for the agent.md prompt."""
    accepted = []
    suggests = {}
    for ev in read_all(repo):
        if ev.get("event") == "suggest":
            suggests[ev.get("txn_id")] = ev
        elif ev.get("event") == "outcome" and ev.get("result") == "accepted":
            s = suggests.get(ev.get("txn_id"))
            if s:
                accepted.append((s.get("payee", ""), s.get("suggestion", "")))
    seen, out = set(), []
    for payee, cat in reversed(accepted):
        if payee not in seen:
            seen.add(payee)
            out.append((payee, cat))
        if len(out) >= limit:
            break
    return out
