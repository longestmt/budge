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
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import __version__, ai, hledger, util
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
accepted. The message value is terminal-native plain text: do not use Markdown,
backticks, or code fences. Use newline escapes for paragraph breaks and put a
command on its own indented line when helpful. Start with a direct one- or
two-sentence answer. For longer answers, put short UPPERCASE section headings
on their own lines, put every ranked item on its own line (for example:
1. Category — detail), use `• ` for bullets, and leave a blank line between
sections. Keep
recommendations concise and scannable. Never include prose outside the JSON
object.
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


def _clean_message(value: object) -> str:
    """Turn model prose into safe, readable terminal-native text."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    # A few providers double-escape content nested in their JSON response.
    text = text.replace("\\n", "\n").replace("\\t", "    ")
    text = re.sub(r"(?m)^[ \t]*```[^\n]*$", "", text)
    # Normalize common Markdown structure before stripping its decoration.
    text = re.sub(
        r"\*\*([^*\n]{2,50}?):\*\*",
        lambda match: "\n\n" + match.group(1).strip().upper() + "\n",
        text,
    )
    text = re.sub(
        r"(?<!\n)[ \t]+(?=(\d+)\.\s+(?:\*\*)?[A-Z])",
        lambda match: "\n\n" if match.group(1) == "1" else "\n",
        text)
    text = re.sub(
        r"[ \t]+[-–—][ \t]+(?=(?:\*\*)?[A-Z][A-Za-z -]{1,30}"
        r"(?:\*\*)?:)",
        "\n• ", text)
    text = re.sub(
        r"[ \t]+(?=(?:Combined,|Together,|Overall,|That's where|"
        r"That means|If you'd like))", "\n\n", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"(?m)^[ \t]*[-*][ \t]+", "• ", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    output = []
    for line in lines:
        if line or not output or output[-1]:
            output.append(line)
    return "\n".join(output).strip()


def _recover_message(raw: str) -> str:
    """Recover only prose from malformed JSON; never recover write actions."""
    match = re.search(r'"message"\s*:\s*', raw)
    if not match:
        return ""
    try:
        value, _ = json.JSONDecoder(strict=False).raw_decode(raw, match.end())
    except (json.JSONDecodeError, TypeError):
        return ""
    return _clean_message(value) if isinstance(value, str) else ""


def _contains_untrusted_action(raw: str) -> bool:
    match = re.search(r'"budget_changes"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
    return bool(match and match.group(1).strip())


def _parse_reply(raw: str, allowed_categories: set[str]) -> TalkReply:
    parsed = ai.extract_json(raw)
    if not isinstance(parsed, dict):
        # A malformed response may still contain a valid JSON string for the
        # human-facing prose. Recover that field alone, never its write action.
        message = _recover_message(raw)
        looks_structured = '"message"' in raw or "```json" in raw.lower()
        if not message and not looks_structured:
            message = _clean_message(raw)
        if not message:
            message = "I couldn't decode that response. Please try again."
        notices = []
        if _contains_untrusted_action(raw):
            notices.append("A budget action was ignored because the model's "
                           "response was malformed.")
        return TalkReply(message=message, notices=notices)

    message = _clean_message(parsed.get("message", ""))
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
        self.context: dict = {}

    def refresh_context(self) -> dict:
        self.context = build_context(self.cfg, self.months)
        return self.context

    def _sync_budget_context(self) -> None:
        """Reflect an applied envelope change without rerunning reports."""
        if not self.context:
            return
        budget = read_budget(Path(self.cfg.repo))
        self.context["current_monthly_budget"] = budget
        current = self.context.get("current_month_to_date", {})
        for category in current.get("categories", []):
            name = category.get("category")
            if name not in budget:
                continue
            amount = budget[name]
            category["monthly_budget"] = amount
            category["remaining"] = round(
                amount - category.get("spent_to_date", 0.0), 2)

    def ask(self, user_message: str) -> TalkReply:
        user_message = user_message.strip()
        if not user_message:
            return TalkReply(message="Ask me anything about your budget.")
        context = self.refresh_context()
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
            if reply.applied:
                self._sync_budget_context()
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
    """Responsive, neon dashboard-style terminal chat interface."""

    def __init__(self, screen, session: TalkSession):
        self.screen = screen
        self.session = session
        self.transcript: list[tuple[str, str]] = [
            ("Budge", "Hi — ask me about your budget, spending patterns, "
             "or a change you'd like to make. Type /help for commands."),
        ]
        self.input_text = ""
        self.status = ""
        self.scroll_offset = 0
        self.theme = {
            "bg": curses.A_NORMAL,
            "brand": curses.A_REVERSE | curses.A_BOLD,
            "green": curses.A_BOLD,
            "purple": curses.A_BOLD,
            "cyan": curses.A_BOLD,
            "yellow": curses.A_BOLD,
            "red": curses.A_BOLD,
            "muted": curses.A_DIM,
            "text": curses.A_NORMAL,
            "composer": curses.A_BOLD,
        }

    def run(self) -> None:
        self._init_theme()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self.screen.keypad(True)
        while True:
            self._draw()
            key = self.screen.get_wch()
            if key == curses.KEY_RESIZE:
                continue
            if key == curses.KEY_PPAGE:
                self.scroll_offset += max(self.screen.getmaxyx()[0] // 2, 1)
                continue
            if key == curses.KEY_NPAGE:
                self.scroll_offset = max(self.scroll_offset
                                         - self.screen.getmaxyx()[0] // 2, 0)
                continue
            if key == curses.KEY_HOME:
                self.scroll_offset = 10 ** 9
                continue
            if key == curses.KEY_END:
                self.scroll_offset = 0
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

    def _init_theme(self) -> None:
        """Use the logo's navy/neon palette with an eight-color fallback."""
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            if getattr(curses, "COLORS", 0) >= 256:
                #             foreground, background
                palette = {
                    1: (252, 17),   # near-white on deep navy
                    2: (48, 53),    # mint on ultraviolet
                    3: (48, 17),    # neon green
                    4: (213, 17),   # hot purple
                    5: (51, 17),    # electric cyan
                    6: (228, 17),   # warning yellow
                    7: (203, 17),   # coral red
                    8: (110, 17),   # muted periwinkle
                    9: (17, 48),    # navy on mint
                    10: (255, 53),  # composer white on purple
                }
            else:
                palette = {
                    1: (curses.COLOR_WHITE, curses.COLOR_BLACK),
                    2: (curses.COLOR_GREEN, curses.COLOR_MAGENTA),
                    3: (curses.COLOR_GREEN, curses.COLOR_BLACK),
                    4: (curses.COLOR_MAGENTA, curses.COLOR_BLACK),
                    5: (curses.COLOR_CYAN, curses.COLOR_BLACK),
                    6: (curses.COLOR_YELLOW, curses.COLOR_BLACK),
                    7: (curses.COLOR_RED, curses.COLOR_BLACK),
                    8: (curses.COLOR_BLUE, curses.COLOR_BLACK),
                    9: (curses.COLOR_BLACK, curses.COLOR_GREEN),
                    10: (curses.COLOR_WHITE, curses.COLOR_MAGENTA),
                }
            for pair, (foreground, background) in palette.items():
                curses.init_pair(pair, foreground, background)
            self.theme = {
                "bg": curses.color_pair(1),
                "brand": curses.color_pair(2) | curses.A_BOLD,
                "green": curses.color_pair(3) | curses.A_BOLD,
                "purple": curses.color_pair(4) | curses.A_BOLD,
                "cyan": curses.color_pair(5) | curses.A_BOLD,
                "yellow": curses.color_pair(6) | curses.A_BOLD,
                "red": curses.color_pair(7) | curses.A_BOLD,
                "muted": curses.color_pair(8) | curses.A_DIM,
                "text": curses.color_pair(1),
                "highlight": curses.color_pair(9) | curses.A_BOLD,
                "composer": curses.color_pair(10) | curses.A_BOLD,
            }
            self.screen.bkgd(" ", self.theme["bg"])
        except curses.error:
            # A terminal advertising broken color support should still work.
            pass

    def _command(self, message: str) -> bool:
        command = message.lower()
        if command in ("/quit", "/exit", "/q"):
            return True
        if command == "/help":
            self.transcript.append((
                "Budge", "/budget shows current envelopes; /clear forgets "
                "this conversation; /quit exits. PgUp/PgDn scroll the chat. "
                "Budget changes requested in chat are checked, audited, "
                "and committed."))
            self.scroll_offset = 0
            return False
        if command == "/clear":
            self.session.clear()
            self.transcript = [("Budge", "Conversation cleared.")]
            self.scroll_offset = 0
            return False
        if command == "/budget":
            budget = read_budget(Path(self.session.cfg.repo))
            text = "\n".join(
                f"{category}: ${amount:,.2f}/month"
                for category, amount in sorted(budget.items()))
            self.transcript.append(("Budget", text or "No envelopes yet."))
            self.scroll_offset = 0
            return False
        self.transcript.append(("You", message))
        self.scroll_offset = 0
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
        if height < 14 or width < 50:
            self._draw_compact(height, width)
            self.screen.refresh()
            return

        self._draw_header(width)
        body_y = 3
        composer_y = height - 4
        body_height = composer_y - body_y
        wide = width >= 100
        sidebar_width = min(37, max(31, width // 3)) if wide else 0
        chat_width = width - sidebar_width - (1 if wide else 0)

        self._box(body_y, 0, body_height, chat_width, " CONVERSATION ",
                  self.theme["purple"])
        self._draw_chat(body_y + 1, 1, body_height - 2, chat_width - 2)
        if wide:
            side_x = chat_width + 1
            self._box(body_y, side_x, body_height, sidebar_width,
                      " LIVE BUDGET ", self.theme["green"])
            self._draw_budget_sidebar(body_y + 1, side_x + 1,
                                      body_height - 2,
                                      sidebar_width - 2)

        self._draw_composer(composer_y, width)
        try:
            prompt_x = 3
            available = max(width - prompt_x - 2, 1)
            shown = self.input_text[-available:]
            self.screen.move(composer_y + 1,
                             min(prompt_x + len(shown), width - 1))
        except curses.error:
            pass
        self.screen.refresh()

    def _draw_header(self, width: int) -> None:
        self._fill(0, 0, width, self.theme["brand"])
        title = f" ░▒▓  BUDGE // TALK  v{__version__} "
        self._add(0, 0, title[:width], self.theme["brand"])
        state = "◆ THINKING" if self.status else "● ONLINE"
        if width > len(title) + len(state) + 2:
            self._add(0, width - len(state) - 1, state,
                      self.theme["brand"])

        self._fill(1, 0, width, self.theme["bg"])
        self._add(1, 2, "PERSONAL FINANCE COMMAND DECK",
                  self.theme["muted"])
        summary = self._header_summary()
        if summary and width > len(summary) + 34:
            self._add(1, width - len(summary) - 2, summary,
                      self.theme["green"])
        horizon = "━" * max(width - 1, 1)
        self._add(2, 0, horizon, self.theme["purple"])
        if width > 10:
            self._add(2, width // 2 - 2, "◆◇◆", self.theme["green"])

    def _header_summary(self) -> str:
        current = self.session.context.get("current_month_to_date", {})
        if not current:
            return ""
        spent = self._money(current.get("spend_to_date", 0.0))
        month = current.get("month", "MTD")
        return f"{month}  SPENT {spent}"

    def _draw_chat(self, y: int, x: int, height: int, width: int) -> None:
        lines = self._chat_lines(width)
        max_offset = max(len(lines) - height, 0)
        self.scroll_offset = min(self.scroll_offset, max_offset)
        end = len(lines) - self.scroll_offset
        start = max(end - height, 0)
        visible = lines[start:end]
        for row, (line, attrs) in enumerate(visible):
            self._add(y + row, x, line[:width], attrs)
        if start > 0:
            self._add(y, x + max(width - 13, 0), " ↑ older ",
                      self.theme["muted"])
        if end < len(lines):
            self._add(y + height - 1, x + max(width - 13, 0),
                      " ↓ newer ", self.theme["muted"])

    def _chat_lines(self, width: int) -> list[tuple[str, int]]:
        output = []
        content_width = max(width - 4, 8)
        role_styles = {
            "You": self.theme["purple"],
            "Budge": self.theme["green"],
            "Applied": self.theme["cyan"],
            "Budget": self.theme["cyan"],
            "Notice": self.theme["yellow"],
            "Error": self.theme["red"],
        }
        icons = {"You": "YOU ◆", "Budge": "BUDGE ◈", "Applied": "APPLIED ✓",
                 "Budget": "BUDGET ▣", "Notice": "NOTICE !",
                 "Error": "ERROR ×"}
        for role, body in self.transcript:
            card_style = role_styles.get(role, self.theme["text"])
            label = icons.get(role, role.upper())
            top_tail = max(width - len(label) - 5, 1)
            output.append((f"╭─ {label} " + "─" * top_tail + "╮",
                           card_style))
            for paragraph in body.splitlines() or [""]:
                wrapped = self._wrap_paragraph(paragraph, content_width)
                for index, line in enumerate(wrapped):
                    line_style = self._message_style(
                        role, paragraph, line, index, card_style)
                    output.append((f"│ {line:<{content_width}} │",
                                   line_style))
            output.append(("╰" + "─" * max(width - 2, 1) + "╯",
                           card_style))
            output.append(("", self.theme["text"]))
        if self.status:
            output.extend([
                ("╭─ BUDGE ◈ " + "─" * max(width - 12, 1) + "╮",
                 self.theme["green"]),
                (f"│ {'Synthesizing your budget context…':<{content_width}} │",
                 self.theme["green"]),
                ("╰" + "─" * max(width - 2, 1) + "╯",
                 self.theme["green"]),
            ])
        return output

    @staticmethod
    def _wrap_paragraph(paragraph: str, width: int) -> list[str]:
        if not paragraph:
            return [""]
        stripped = paragraph.strip()
        subsequent = ""
        number = re.match(r"(\d+\.\s+)", stripped)
        if number:
            subsequent = " " * len(number.group(1))
        elif stripped.startswith("• "):
            subsequent = "  "
        elif paragraph.startswith(("  ", "\t")):
            stripped = "  " + stripped
            subsequent = "  "
        return textwrap.wrap(
            stripped, width=width, subsequent_indent=subsequent,
            replace_whitespace=False, drop_whitespace=True,
            break_long_words=False, break_on_hyphens=False) or [""]

    def _message_style(self, role: str, paragraph: str, line: str,
                       index: int, card_style: int) -> int:
        if role != "Budge":
            return card_style
        stripped = line.strip()
        original = paragraph.strip()
        if (original and len(original) <= 60
                and original == original.upper()
                and re.search(r"[A-Z]", original)):
            return self.theme["purple"]
        if index == 0 and re.match(r"\d+\.\s+", stripped):
            return self.theme["cyan"]
        if index == 0 and stripped.startswith("• "):
            return self.theme["green"]
        if stripped.startswith(("hledger ", "budge ")):
            return self.theme["yellow"]
        return self.theme["text"]

    def _draw_budget_sidebar(self, y: int, x: int,
                             height: int, width: int) -> None:
        context = self.session.context
        current = context.get("current_month_to_date", {})
        household = context.get("household", {})
        month = current.get("month", "NO DATA")
        through = current.get("partial_through", "")
        self._add(y, x + 1, f"{month}  MTD", self.theme["green"])
        if through:
            self._add(y + 1, x + 1, f"through {through}",
                      self.theme["muted"])
        self._add(y + 3, x + 1, "SPENT", self.theme["muted"])
        self._add(y + 3, x + max(width // 2, 9),
                  self._money(current.get("spend_to_date", 0.0)),
                  self.theme["green"])
        self._add(y + 4, x + 1, "INCOME", self.theme["muted"])
        self._add(y + 4, x + max(width // 2, 9),
                  self._money(current.get("income_to_date", 0.0)),
                  self.theme["cyan"])
        target = household.get("monthly_savings_target", 0.0)
        self._add(y + 5, x + 1, "SAVE GOAL", self.theme["muted"])
        self._add(y + 5, x + max(width // 2, 9), self._money(target),
                  self.theme["purple"])
        self._add(y + 7, x + 1, "ENVELOPES", self.theme["purple"])

        categories = list(current.get("categories", []))
        categories.sort(key=lambda row: self._ratio(
            row.get("spent_to_date", 0.0), row.get("monthly_budget", 0.0)),
                        reverse=True)
        row_y = y + 9
        room = max((height - 9) // 2, 0)
        shown = categories[:room]
        for category in shown:
            name = str(category.get("category", "")).removeprefix("expenses:")
            budget = category.get("monthly_budget", 0.0)
            spent = category.get("spent_to_date", 0.0)
            ratio = self._ratio(spent, budget)
            style = (self.theme["red"] if ratio > 1
                     else self.theme["yellow"] if ratio >= .8
                     else self.theme["green"])
            money = f"{self._money(spent)}/{self._money(budget)}"
            available = max(width - len(money) - 3, 4)
            self._add(row_y, x + 1, name[:available], self.theme["text"])
            self._add(row_y, x + max(width - len(money) - 1, 1), money,
                      style)
            bar_width = max(width - 2, 4)
            self._add(row_y + 1, x + 1,
                      self._progress(ratio, bar_width), style)
            row_y += 2
        remaining = len(categories) - len(shown)
        if remaining > 0 and row_y < y + height:
            self._add(row_y, x + 1, f"+ {remaining} more  •  /budget",
                      self.theme["muted"])

    @staticmethod
    def _ratio(spent: float, budget: float) -> float:
        if budget <= 0:
            return 1.0 if spent > 0 else 0.0
        return spent / budget

    @staticmethod
    def _progress(ratio: float, width: int) -> str:
        filled = min(max(round(ratio * width), 0), width)
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def _money(value: float) -> str:
        value = float(value or 0.0)
        if abs(value) >= 10000:
            return f"${value / 1000:,.1f}k"
        return f"${value:,.0f}"

    def _draw_composer(self, y: int, width: int) -> None:
        status = " THINKING ◌ " if self.status else " READY ◆ "
        self._box(y, 0, 3, width, " MESSAGE ", self.theme["purple"],
                  right_title=status)
        prompt = "› "
        available = max(width - len(prompt) - 3, 1)
        shown = self.input_text[-available:]
        self._fill(y + 1, 1, width - 2, self.theme["composer"])
        self._add(y + 1, 1, prompt, self.theme["composer"])
        self._add(y + 1, 3, shown, self.theme["composer"])
        footer = " ENTER send   PGUP/PGDN scroll   /budget   /clear   ESC quit "
        self._fill(y + 3, 0, width, self.theme["brand"])
        self._add(y + 3, 1, footer[:max(width - 2, 1)],
                  self.theme["brand"])

    def _draw_compact(self, height: int, width: int) -> None:
        width = max(width, 1)
        self._fill(0, 0, width, self.theme["brand"])
        self._add(0, 0, " BUDGE // TALK ", self.theme["brand"])
        body_height = max(height - 3, 1)
        lines = self._chat_lines(max(width - 1, 8))
        max_offset = max(len(lines) - body_height, 0)
        self.scroll_offset = min(self.scroll_offset, max_offset)
        end = len(lines) - self.scroll_offset
        start = max(end - body_height, 0)
        for row, (line, attrs) in enumerate(lines[start:end], start=1):
            self._add(row, 0, line[:max(width - 1, 1)], attrs)
        input_y = max(height - 2, 1)
        self._add(input_y, 0, "› ", self.theme["green"])
        self._add(input_y, 2, self.input_text[-max(width - 3, 1):],
                  self.theme["composer"])
        self._fill(max(height - 1, 1), 0, width, self.theme["brand"])
        self._add(max(height - 1, 1), 1, "ENTER send • ESC quit",
                  self.theme["brand"])

    def _box(self, y: int, x: int, height: int, width: int, title: str,
             attrs: int, right_title: str = "") -> None:
        if height < 2 or width < 4:
            return
        top = "╭" + "─" * (width - 2) + "╮"
        bottom = "╰" + "─" * (width - 2) + "╯"
        self._add(y, x, top, attrs)
        self._add(y + height - 1, x, bottom, attrs)
        for row in range(y + 1, y + height - 1):
            self._add(row, x, "│", attrs)
            self._add(row, x + width - 1, "│", attrs)
        self._add(y, x + 2, title[:max(width - 4, 1)], attrs)
        if right_title and width > len(title) + len(right_title) + 6:
            self._add(y, x + width - len(right_title) - 2,
                      right_title, attrs)

    def _fill(self, y: int, x: int, width: int, attrs: int) -> None:
        self._add(y, x, " " * max(width, 0), attrs)

    def _add(self, y: int, x: int, text: str, attrs: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if y < 0 or x < 0 or y >= height or x >= width:
            return
        text = text[:max(width - x, 0)]
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
    session.refresh_context()
    curses.wrapper(lambda screen: TalkTUI(screen, session).run())
