"""Interactive budget conversation — ``budge talk``.

The full-screen interface is deliberately built on :mod:`curses` so Budge
keeps its zero-runtime-dependency promise.  The model receives the same
minimized, aggregate financial context as ``budge consult`` and can request
changes to existing monthly envelopes.  Those changes are validated locally,
written through the hledger check gate, audited, and committed.
"""

from __future__ import annotations

import curses
import datetime as dt
import json
import math
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import ai, hledger, util
from .consult import (_amount, _budget_category, _register_rows,
                      build_snapshot)
from .gitutil import commit_all
from .plan import (_render_budget_text, append_decision, read_budget,
                   read_household)
from .util import note, write_file


TALK_SYSTEM = """\
You are Budge Talk: a thoughtful household budget assistant and an expert on
Budge and hledger. Keep the conversation practical, concise, kind, and
grounded in the household context supplied with each turn and the product
reference below. You may explain spending patterns, compare actual spending
with envelopes, explore tradeoffs, help the user decide how to allocate their
money, explain how Budge works, and teach the user how to inspect their books
with hledger. Do not use outside spending benchmarks, invent transactions, or
imply that aggregate data is more precise than it is. Transfers are not
spending. Clearly distinguish completed-month averages from the current
monthly budget and the partial current-month totals. Do not present yourself
as a financial adviser.

## Budge and hledger product reference

- Budge is orchestration around stock hledger, SimpleFIN, and Paisa. The
  household's private data repository contains ordinary journal, CSV, Markdown,
  and rules files; Budge does not replace hledger's reporting interface.
- `main.journal` is the source of truth and includes `accounts.journal`,
  `budget.journal`, and `pending.journal`. `accounts.journal` declares accounts.
  `budget.journal` represents monthly envelopes with an hledger periodic
  transaction. `household.md` stores stated income, savings goals, context, and
  an append-only budget decision log.
- `budge fetch` pulls SimpleFIN transactions into immutable raw CSV files.
  Deterministic CSV-rule matches enter `main.journal` cleared (`*`); unmatched
  transactions enter `pending.journal` pending (`!`). Card payments and
  transfers post through `assets:transfers` and are not expenses.
- `budge categorize` suggests categories for unmatched pending transactions.
  `budge review` approves or corrects them, and vendor corrections become
  durable CSV rules. `budge promote` runs the hledger check gate, changes
  approved pending entries to cleared, and moves them into `main.journal`.
  `budge regenerate` rebuilds derived pending data from raw CSVs, rules, and
  logged decisions.
- `budge plan` creates or reassesses the household budget. `budge consult`
  prepares a completed-month cashflow proposal. `budge talk` is this
  conversation. Talk's only action is setting existing monthly envelope
  amounts; it cannot edit transactions, rules, or account declarations.
- Use hledger directly for questions and reports. Reliable examples:
  - Validate the books: `hledger -f main.journal check`
  - Monthly budget view: `hledger -f main.journal balance --budget -M expenses`
  - Dining activity: `hledger -f main.journal register expenses:dining`
  - Transfer activity: `hledger -f main.journal register assets:transfers`
  `budge cheatsheet` prints more curated recipes. There is deliberately no
  `budge balance` or `budge report`.
- Explain double-entry accounting, account queries, statuses, periodic budget
  transactions, and hledger output in plain language. Never invent Budge
  commands or hledger flags. If a requested hledger operation is not covered by
  this reference and you are unsure of its exact syntax, say so and recommend
  `hledger --help`, `hledger COMMAND --help`, or `budge cheatsheet`.

You are authorized to change the household's existing monthly budget
envelopes. Emit a budget change when the user clearly asks for one or
explicitly accepts one discussed in the conversation; no further confirmation
is needed. Do not change a budget merely because you recommend it. If the
request is ambiguous, ask a concise follow-up question. You may only set an
existing category to a finite, non-negative monthly amount. Budge validates,
checks, audits, and commits every requested change outside the model, so never
claim that a change succeeded in your message.

Reply with ONLY one JSON object in this shape:
{
  "message": "your conversational response",
  "budget_changes": [
    {
      "category": "expenses:dining",
      "monthly_amount": 250.00,
      "reason": "short explanation tied to the conversation"
    }
  ]
}
Use an empty budget_changes list when no change was clearly requested or
accepted. Never include prose outside the JSON object.
"""


@dataclass
class BudgetChange:
    category: str
    monthly_amount: float
    reason: str = ""


@dataclass
class TalkReply:
    message: str
    requested: list[BudgetChange] = field(default_factory=list)
    applied: list[BudgetChange] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def build_context(cfg, months: int = 6) -> dict:
    """Build the minimized household context sent with each talk turn."""
    repo = Path(cfg.repo)
    budget = read_budget(repo)
    household = read_household(repo)
    snapshot = build_snapshot(repo, budget, months) if budget else {
        "months": [], "categories": []}
    context = {
        "household": {
            "monthly_take_home": household["income"],
            "monthly_savings_target": household["savings"],
            "goals_and_upcoming_changes": household["goals"],
        },
        "current_monthly_budget": budget,
        "current_month_to_date": _current_month_context(repo, budget),
        "completed_month_analysis": snapshot,
    }
    return context


def _current_month_context(repo: Path, budget: dict,
                           today: Optional[dt.date] = None) -> dict:
    """Return category and merchant aggregates for the partial current month."""
    today = today or dt.date.today()
    start = today.replace(day=1)
    end = today + dt.timedelta(days=1)  # hledger's -e boundary is exclusive
    expense_rows = _register_rows(repo, "expenses", start, end)
    income_rows = _register_rows(repo, "income", start, end)
    spent = {category: 0.0 for category in budget}
    merchants: dict[str, dict[str, float]] = {
        category: {} for category in budget}
    total_spend = unbudgeted = 0.0
    for row in expense_rows:
        value = _amount(row.get("amount", ""))
        total_spend += value
        category = _budget_category(row.get("account", ""), budget)
        if category is None:
            unbudgeted += value
            continue
        spent[category] += value
        payee = row.get("description", "").strip()
        if payee and value > 0:
            merchants[category][payee] = (
                merchants[category].get(payee, 0.0) + value)
    income = sum(-_amount(row.get("amount", "")) for row in income_rows)
    categories = []
    for category, amount in sorted(budget.items()):
        category_spend = spent[category]
        top = sorted(merchants[category].items(), key=lambda item: -item[1])
        categories.append({
            "category": category,
            "monthly_budget": round(amount, 2),
            "spent_to_date": round(category_spend, 2),
            "remaining": round(amount - category_spend, 2),
            "top_merchants": [
                {"payee": payee, "spent_to_date": round(value, 2)}
                for payee, value in top[:5]
            ],
        })
    return {
        "month": start.strftime("%Y-%m"),
        "partial_through": today.isoformat(),
        "income_to_date": round(income, 2),
        "spend_to_date": round(total_spend, 2),
        "unbudgeted_spend_to_date": round(unbudgeted, 2),
        "categories": categories,
    }


def _parse_reply(raw: str, allowed_categories: set[str]) -> TalkReply:
    parsed = ai.extract_json(raw)
    if not isinstance(parsed, dict):
        # A provider that ignores the JSON contract can still converse, but
        # must never gain a write path through malformed output.
        message = (raw.strip()
                   or "I couldn't produce a response. Please retry.")
        return TalkReply(message=message,
                         notices=["No budget action was accepted from the "
                                  "unstructured model response."])

    message = " ".join(str(parsed.get("message", "")).split())
    if not message:
        message = "What would you like to explore about your budget?"

    changes: dict[str, BudgetChange] = {}
    notices = []
    proposed = parsed.get("budget_changes", [])
    if not isinstance(proposed, list):
        proposed = []
        notices.append("Ignored a malformed budget change list.")
    for item in proposed:
        if not isinstance(item, dict):
            notices.append("Ignored a malformed budget change.")
            continue
        category = str(item.get("category", "")).strip()
        if category not in allowed_categories:
            shown_category = category or "(missing)"
            notices.append(
                f"Rejected change for unknown category: {shown_category}.")
            continue
        try:
            if isinstance(item.get("monthly_amount"), bool):
                raise TypeError
            amount = float(item.get("monthly_amount"))
        except (TypeError, ValueError):
            notices.append(f"Rejected non-numeric amount for {category}.")
            continue
        if not math.isfinite(amount) or amount < 0:
            notices.append(f"Rejected invalid amount for {category}.")
            continue
        changes[category] = BudgetChange(
            category=category,
            monthly_amount=round(amount, 2),
            reason=" ".join(str(item.get("reason", "")).split())[:300],
        )
    return TalkReply(message=message, requested=list(changes.values()),
                     notices=notices)


def apply_budget_changes(
        cfg, changes: list[BudgetChange]) -> list[BudgetChange]:
    """Apply valid envelope changes atomically through the normal hard gate."""
    if not changes:
        return []
    repo = Path(cfg.repo)
    budget = read_budget(repo)
    if not budget:
        raise RuntimeError("no budget envelopes exist; run `budge plan` first")

    normalized: dict[str, BudgetChange] = {}
    for change in changes:
        if change.category not in budget:
            raise RuntimeError(
                f"unknown budget category: {change.category or '(missing)'}")
        try:
            if isinstance(change.monthly_amount, bool):
                raise TypeError
            amount = float(change.monthly_amount)
        except (TypeError, ValueError):
            raise RuntimeError(
                f"non-numeric amount for {change.category}") from None
        if not math.isfinite(amount) or amount < 0:
            raise RuntimeError(f"invalid amount for {change.category}")
        normalized[change.category] = BudgetChange(
            category=change.category, monthly_amount=round(amount, 2),
            reason=" ".join(str(change.reason).split())[:300])
    effective = [c for c in normalized.values()
                 if budget[c.category] != c.monthly_amount]
    if not effective:
        return []

    old_path = repo / "budget.journal"
    old_text = old_path.read_text(encoding="utf-8")
    old_amounts = {c.category: budget[c.category] for c in effective}
    new_budget = dict(budget)
    for change in effective:
        new_budget[change.category] = change.monthly_amount

    write_file(old_path, _render_budget_text(cfg, new_budget))
    ok, output = hledger.check(repo / "main.journal")
    if not ok:
        write_file(old_path, old_text)
        raise RuntimeError("hledger rejected the budget change; the budget "
                           "was rolled back" + (f":\n{output}" if output else ""))

    append_decision(repo, "budge talk", [
        f"{change.category}: ${old_amounts[change.category]:.2f} -> "
        f"${change.monthly_amount:.2f} — "
        + (change.reason or "changed during authorized conversation")
        + " — APPLIED"
        for change in effective
    ])
    commit_all(repo, "budge talk: budget update")
    return effective


class TalkSession:
    """Provider conversation plus the narrow, checked budget write surface."""

    def __init__(self, cfg, months: int = 6):
        if months < 1:
            raise ValueError("months must be at least 1")
        self.cfg = cfg
        self.months = months
        self.history: list[dict[str, str]] = []

    def ask(self, user_message: str) -> TalkReply:
        user_message = user_message.strip()
        if not user_message:
            return TalkReply(message="Ask me anything about your budget.")
        context = build_context(self.cfg, self.months)
        payload = {
            "household_context": context,
            "conversation": self.history[-20:],
            "user_message": user_message,
        }
        raw = ai.complete(self.cfg, TALK_SYSTEM,
                          json.dumps(payload, ensure_ascii=False))
        reply = _parse_reply(
            raw, set(context["current_monthly_budget"].keys()))
        try:
            reply.applied = apply_budget_changes(self.cfg, reply.requested)
            unchanged = {c.category for c in reply.requested} - {
                c.category for c in reply.applied}
            for category in sorted(unchanged):
                reply.notices.append(
                    f"{category} was already set to that amount.")
        except RuntimeError as exc:
            reply.notices.append(f"Budget change failed: {exc}")
        self.history.extend([
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": reply.message},
        ])
        return reply

    def clear(self) -> None:
        self.history.clear()


class TalkTUI:
    """Small scrolling, full-screen terminal chat interface."""

    def __init__(self, screen, session: TalkSession):
        self.screen = screen
        self.session = session
        self.transcript: list[tuple[str, str]] = [
            ("Budge", "Hi — ask me about your budget, spending patterns, "
             "or a change you'd like to make. Type /help for commands."),
        ]
        self.input_text = ""
        self.status = ""

    def run(self) -> None:
        curses.curs_set(1)
        self.screen.keypad(True)
        while True:
            self._draw()
            key = self.screen.get_wch()
            if key == curses.KEY_RESIZE:
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                message = self.input_text.strip()
                self.input_text = ""
                if not message:
                    continue
                if self._command(message):
                    return
                continue
            if key in ("\x04", "\x1b"):
                return
            if key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                self.input_text = self.input_text[:-1]
            elif isinstance(key, str) and key.isprintable():
                self.input_text += key

    def _command(self, message: str) -> bool:
        command = message.lower()
        if command in ("/quit", "/exit", "/q"):
            return True
        if command == "/help":
            self.transcript.append((
                "Budge", "/budget shows current envelopes; /clear forgets "
                "this conversation; /quit exits. Budget changes requested "
                "in chat are checked, audited, and committed."))
            return False
        if command == "/clear":
            self.session.clear()
            self.transcript = [("Budge", "Conversation cleared.")]
            return False
        if command == "/budget":
            budget = read_budget(Path(self.session.cfg.repo))
            text = "\n".join(
                f"{category}: ${amount:,.2f}/month"
                for category, amount in sorted(budget.items()))
            self.transcript.append(("Budget", text or "No envelopes yet."))
            return False
        self.transcript.append(("You", message))
        self.status = "Budge is thinking..."
        self._draw()
        try:
            reply = self.session.ask(message)
            self.transcript.append(("Budge", reply.message))
            for change in reply.applied:
                self.transcript.append((
                    "Applied", f"{change.category} -> "
                    f"${change.monthly_amount:,.2f}/month"))
            for notice_text in reply.notices:
                self.transcript.append(("Notice", notice_text))
        except ai.AIError as exc:
            self.transcript.append(("Error", str(exc)))
        finally:
            self.status = ""
        return False

    def _draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        width = max(width, 12)
        title = " budge talk "
        subtitle = " /help  /budget  /quit "
        self._add(0, 0, title[:width], curses.A_REVERSE | curses.A_BOLD)
        if width > len(subtitle) + len(title):
            self._add(0, width - len(subtitle), subtitle, curses.A_REVERSE)

        body_height = max(height - 3, 1)
        lines = []
        wrap_width = max(width - 4, 8)
        for role, body in self.transcript:
            parts = body.splitlines() or [""]
            wrapped = []
            for part in parts:
                wrapped.extend(textwrap.wrap(
                    part, width=wrap_width, replace_whitespace=False,
                    drop_whitespace=True) or [""])
            lines.append((role + ":", curses.A_BOLD))
            lines.extend(("  " + line, curses.A_NORMAL) for line in wrapped)
            lines.append(("", curses.A_NORMAL))
        if self.status:
            lines.append((self.status, curses.A_DIM))
        visible = lines[-body_height:]
        for row, (line, attrs) in enumerate(visible, start=1):
            self._add(row, 0, line[:max(width - 1, 1)], attrs)

        prompt = "> "
        available = max(width - len(prompt) - 1, 1)
        shown = self.input_text[-available:]
        input_row = max(height - 1, 1)
        self._add(input_row, 0, prompt, curses.A_BOLD)
        self._add(input_row, len(prompt), shown)
        try:
            self.screen.move(input_row, min(len(prompt) + len(shown), width - 1))
        except curses.error:
            pass
        self.screen.refresh()

    def _add(self, y: int, x: int, text: str, attrs: int = 0) -> None:
        try:
            self.screen.addstr(y, x, text, attrs)
        except curses.error:
            # Tiny terminals and bottom-right cells can make curses raise.
            pass


def run_talk(cfg, months: int = 6) -> None:
    """Open the Budge Talk full-screen terminal interface."""
    if months < 1:
        raise RuntimeError("--months must be at least 1")
    if util.DRY_RUN:
        note("[dry-run] would open Budge Talk; no AI call or write was made")
        return
    if not read_budget(Path(cfg.repo)):
        raise RuntimeError("no budget.journal envelopes yet — run `budge plan` "
                           "first")
    import sys
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("`budge talk` needs an interactive terminal")
    session = TalkSession(cfg, months=months)
    curses.wrapper(lambda screen: TalkTUI(screen, session).run())
