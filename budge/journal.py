"""Journal entry model for pending.journal (a budge-DERIVED artifact).

pending.journal is written and parsed only by budge — it is regenerable from
raw CSVs + rules + the AI decision log at any time. main.journal is hledger's
domain; budge only ever appends hledger-formatted text to it.

Pending entry format (status `!` per PRD section 4):

    2026-06-08 ! AMAZON MKTPLACE
        ; simplefin_id: TXN-123
        ; source_account: assets:checking
        ; ai: confidence=medium
        ; rationale: online retail purchase
        expenses:shopping      $43.21
        assets:checking       $-43.21
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

UNCATEGORIZED = "expenses:uncategorized"
TRANSFERS = "assets:transfers"
OPENING = "equity:opening-balances"

PENDING_HEADER = (
    "; pending.journal — DERIVED FILE, managed by budge.\n"
    "; AI-categorized transactions awaiting review (status !).\n"
    "; Do not hand-edit unless via `budge review --edit`; regeneration may\n"
    "; rebuild this file from import/raw + rules + ai/ai-decisions.log.\n\n"
)


@dataclass
class Pending:
    date: str
    payee: str
    sf_id: str
    source_account: str
    amount: str            # e.g. "$-43.21" — amount on the source account
    category: str = UNCATEGORIZED
    confidence: str = ""   # high | medium | low | "" (not yet AI-touched)
    rationale: str = ""
    origin: str = "ai"     # ai | rule | manual | uncategorized
    suggested: str = ""    # the AI's original suggestion (for accepted/overridden)

    def render(self) -> str:
        lines = [f"{self.date} ! {self.payee}"]
        lines.append(f"    ; simplefin_id: {self.sf_id}")
        lines.append(f"    ; source_account: {self.source_account}")
        lines.append(f"    ; origin: {self.origin}")
        if self.suggested:
            lines.append(f"    ; suggested: {self.suggested}")
        if self.confidence:
            lines.append(f"    ; ai: confidence={self.confidence}")
        if self.rationale:
            lines.append(f"    ; rationale: {self.rationale}")
        lines.append(f"    {self.category:<40s}  {negate(self.amount)}")
        lines.append(f"    {self.source_account:<40s}  {self.amount}")
        return "\n".join(lines) + "\n"


def negate(amount: str) -> str:
    amount = amount.strip()
    if amount.startswith("-"):
        return amount[1:]
    if re.match(r"^\$-", amount):
        return "$" + amount[2:]
    if amount.startswith("$"):
        return "$-" + amount[1:]
    return "-" + amount


def parse_pending(path: Path) -> list:
    """Parse pending.journal back into Pending objects."""
    if not Path(path).exists():
        return []
    text = Path(path).read_text(encoding="utf-8")
    entries = []
    current = None
    for line in text.splitlines():
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+!\s+(.*)$", line)
        if m:
            if current:
                entries.append(current)
            current = Pending(date=m.group(1), payee=m.group(2).strip(),
                              sf_id="", source_account="", amount="")
            continue
        if current is None:
            continue
        s = line.strip()
        if s.startswith("; simplefin_id:"):
            current.sf_id = s.split(":", 1)[1].strip()
        elif s.startswith("; source_account:"):
            current.source_account = s.split(":", 1)[1].strip()
        elif s.startswith("; origin:"):
            current.origin = s.split(":", 1)[1].strip()
        elif s.startswith("; suggested:"):
            current.suggested = s.split(":", 1)[1].strip()
        elif s.startswith("; ai:"):
            m2 = re.search(r"confidence=(\w+)", s)
            if m2:
                current.confidence = m2.group(1)
        elif s.startswith("; rationale:"):
            current.rationale = s.split(":", 1)[1].strip()
        elif s and not s.startswith(";"):
            # posting line: "account  amount"
            pm = re.match(r"^(\S[\S ]*?)\s{2,}(\S.*)$", s)
            if pm:
                account, amount = pm.group(1).strip(), pm.group(2).strip()
                if account == current.source_account:
                    current.amount = amount
                else:
                    current.category = account
    if current:
        entries.append(current)
    return entries


def write_pending(path: Path, entries: list) -> None:
    from .util import write_file

    body = "\n".join(e.render() for e in entries)
    write_file(path, PENDING_HEADER + body)


def render_cleared(entry: Pending) -> str:
    """Render a pending entry as a cleared (*) main.journal entry."""
    lines = [f"{entry.date} * {entry.payee}"]
    lines.append(f"    ; simplefin_id: {entry.sf_id}")
    if entry.origin == "ai":
        lines.append("    ; categorized_by: ai, approved in review")
    elif entry.origin == "manual":
        lines.append("    ; categorized_by: operator (review)")
    lines.append(f"    {entry.category:<40s}  {negate(entry.amount)}")
    lines.append(f"    {entry.source_account:<40s}  {entry.amount}")
    return "\n".join(lines) + "\n"


def journal_sf_ids(path: Path) -> set:
    """All simplefin_id tags present in a journal file (and only that file)."""
    if not Path(path).exists():
        return set()
    text = Path(path).read_text(encoding="utf-8")
    return set(re.findall(r"simplefin_id:\s*(\S+)", text))
