"""Cashflow-aware budget consultation — ``budge consult``.

The consultation uses completed calendar months only, compares the resulting
monthly averages with the current envelopes, and asks the configured AI for a
grounded proposal.  The model sees aggregates and merchant totals, not full
transactions or account balances.  Budge computes all savings arithmetic and
keeps the existing explicit-confirmation + hledger-check write gate.
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import io
import json
import math
import re
from pathlib import Path
from typing import Optional

from . import ai, hledger, util
from .gitutil import commit_all
from .plan import _render_budget_text, append_decision, read_budget, read_household
from .util import banner, confirm, die, header, note, paint, say, warn, write_file

CONSULT_SYSTEM = """\
You are a household budget consultant. Use only the household's supplied
cashflow, category averages, current envelopes, and merchant aggregates. Do
not use external benchmarks or invent facts.

Propose a realistic monthly envelope for EVERY supplied category. A proposal
may raise an envelope when the current budget is consistently unrealistic,
keep it unchanged, or lower it when the data supports a practical reduction.
Suggestions to reduce spending must be specific, kind, and actionable. Refer
to a supplied merchant by name only when that merchant actually appears in
the category. Never suggest changing transfers because they are excluded.
Keep the complete proposal within the supplied spending ceiling when
possible, while acknowledging that essential categories may limit cuts.

Reply with ONLY a JSON object:
{
  "summary": "one concise paragraph",
  "categories": [
    {
      "category": "expenses:dining",
      "proposed_monthly": 250.00,
      "suggestion": "one concrete suggestion, or empty when no change"
    }
  ]
}
"""


def _shift_month(first: dt.date, delta: int) -> dt.date:
    """Shift a first-of-month date by ``delta`` calendar months."""
    serial = first.year * 12 + first.month - 1 + delta
    return dt.date(serial // 12, serial % 12 + 1, 1)


def _amount(cell: str) -> float:
    """Parse the single-commodity amounts emitted by hledger CSV."""
    cleaned = cell.replace(",", "").replace("$", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(m.group(0)) if m else 0.0


def _register_rows(repo: Path, root: str, start: dt.date,
                   end: dt.date) -> list[dict]:
    proc = hledger.hledger([
        "register", "-f", Path(repo) / "main.journal", root,
        "-b", start.isoformat(), "-e", end.isoformat(), "-O", "csv",
    ], check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"hledger could not read {root} cashflow:\n"
            + (proc.stdout + proc.stderr).strip()
        )
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def _budget_category(account: str, budget: dict) -> Optional[str]:
    matches = [cat for cat in budget
               if account == cat or account.startswith(cat + ":")]
    return max(matches, key=len) if matches else None


def collect_history(repo: Path, budget: dict, months: int,
                    today: Optional[dt.date] = None) -> dict:
    """Collect completed-month cashflow and merchant aggregates.

    The requested window is shortened to the first month containing available
    income or spend data. Zero-spend months *inside* that available span still
    count in averages; the current partial month never does.
    """
    if months < 1:
        raise ValueError("months must be at least 1")
    today = today or dt.date.today()
    end = today.replace(day=1)
    requested_start = _shift_month(end, -months)
    expense_rows = _register_rows(repo, "expenses", requested_start, end)
    income_rows = _register_rows(repo, "income", requested_start, end)
    dated_rows = [r for r in expense_rows + income_rows if r.get("date")]
    if not dated_rows:
        return {"months": [], "categories": {}, "income": [],
                "spend": [], "unbudgeted": [], "merchants": {}}

    earliest = min(dt.date.fromisoformat(r["date"]).replace(day=1)
                   for r in dated_rows)
    start = max(requested_start, earliest)
    labels = []
    cursor = start
    while cursor < end:
        labels.append(cursor.strftime("%Y-%m"))
        cursor = _shift_month(cursor, 1)
    index = {label: i for i, label in enumerate(labels)}

    categories = {cat: [0.0] * len(labels) for cat in budget}
    spend = [0.0] * len(labels)
    unbudgeted = [0.0] * len(labels)
    income = [0.0] * len(labels)
    merchants: dict[str, dict[str, float]] = {cat: {} for cat in budget}

    for row in expense_rows:
        month = row.get("date", "")[:7]
        if month not in index:
            continue
        value = _amount(row.get("amount", ""))
        spend[index[month]] += value
        cat = _budget_category(row.get("account", ""), budget)
        if cat is None:
            unbudgeted[index[month]] += value
            continue
        categories[cat][index[month]] += value
        payee = row.get("description", "").strip()
        if payee and value > 0:
            merchants[cat][payee] = merchants[cat].get(payee, 0.0) + value

    for row in income_rows:
        month = row.get("date", "")[:7]
        if month in index:
            # Income postings are normally negative in double-entry books.
            income[index[month]] += -_amount(row.get("amount", ""))

    count = max(len(labels), 1)
    merchant_monthly = {
        cat: [
            {"payee": payee, "monthly_average": round(total / count, 2)}
            for payee, total in sorted(values.items(),
                                       key=lambda item: -item[1])[:5]
        ]
        for cat, values in merchants.items()
    }
    return {"months": labels, "categories": categories, "income": income,
            "spend": spend, "unbudgeted": unbudgeted,
            "merchants": merchant_monthly}


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_snapshot(repo: Path, budget: dict, months: int,
                   today: Optional[dt.date] = None) -> dict:
    history = collect_history(repo, budget, months, today=today)
    if not history["months"]:
        return {"months": [], "categories": []}
    household = read_household(repo)
    observed_income = _average(history["income"])
    observed_spend = _average(history["spend"])
    unbudgeted = _average(history["unbudgeted"])
    stated_income = household["income"]
    baseline_income = observed_income if observed_income > 0 else stated_income
    savings_target = household["savings"]
    ceiling = max(baseline_income - savings_target, 0.0)
    categories = []
    for cat, current in sorted(budget.items()):
        avg = _average(history["categories"].get(cat, []))
        categories.append({
            "category": cat,
            "current_budget": round(current, 2),
            # "Adjusted" means a completed-calendar-month average across the
            # available span. It includes internal zero-spend months.
            "adjusted_average": round(avg, 2),
            "monthly_actuals": [round(value, 2) for value in
                                history["categories"].get(cat, [])],
            "top_merchants": history["merchants"].get(cat, []),
        })
    return {
        "months": history["months"],
        "categories": categories,
        "observed_income": round(observed_income, 2),
        "stated_income": round(stated_income, 2),
        "income_basis": round(baseline_income, 2),
        "observed_spend": round(observed_spend, 2),
        "savings_target": round(savings_target, 2),
        "household_goals": household["goals"],
        "spending_ceiling": round(ceiling, 2),
        "current_budget_total": round(sum(budget.values()), 2),
        "unbudgeted_average": round(unbudgeted, 2),
        "cashflow_after_spend_and_savings": round(
            baseline_income - observed_spend - savings_target, 2),
    }


def _fallback_amount(category: dict) -> float:
    avg = category["adjusted_average"]
    if avg <= 0:
        return category["current_budget"]
    return math.ceil(avg / 10.0) * 10.0


def validate_proposal(parsed: object, snapshot: dict) -> tuple[str, list[dict]]:
    """Validate model output and fill omissions with an arithmetic baseline."""
    parsed = parsed if isinstance(parsed, dict) else {}
    summary = " ".join(str(parsed.get("summary", "")).split())[:500]
    supplied = {}
    for item in parsed.get("categories", []):
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category", "")).strip()
        try:
            amount = float(item.get("proposed_monthly"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(amount) or amount < 0:
            continue
        supplied[cat] = {
            "proposed_monthly": round(amount, 2),
            "suggestion": " ".join(
                str(item.get("suggestion", "")).split())[:400],
        }

    proposal = []
    for category in snapshot["categories"]:
        cat = category["category"]
        model = supplied.get(cat, {})
        proposed = model.get("proposed_monthly", _fallback_amount(category))
        avg = category["adjusted_average"]
        monthly_savings = max(avg - proposed, 0.0)
        proposal.append({
            **category,
            "proposed_monthly": round(proposed, 2),
            "budget_change": round(proposed - category["current_budget"], 2),
            "monthly_savings": round(monthly_savings, 2),
            "annual_savings": round(monthly_savings * 12, 2),
            "suggestion": model.get("suggestion", ""),
        })
    return summary, proposal


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _show_snapshot(snapshot: dict) -> None:
    header("cashflow baseline")
    first, last = snapshot["months"][0], snapshot["months"][-1]
    note(f"Completed months only: {first} through {last} "
         f"({len(snapshot['months'])} month(s)); zero-spend months inside "
         "that span count in the averages.")
    say(f"  observed income / month        {_money(snapshot['observed_income'])}")
    if snapshot["stated_income"]:
        say(f"  household stated income       {_money(snapshot['stated_income'])}")
    say(f"  observed spending / month      {_money(snapshot['observed_spend'])}")
    say(f"  savings target / month         {_money(snapshot['savings_target'])}")
    say(f"  spending ceiling               {_money(snapshot['spending_ceiling'])}")
    say(f"  current envelope total         {_money(snapshot['current_budget_total'])}")
    remainder = snapshot["cashflow_after_spend_and_savings"]
    style = "green" if remainder >= 0 else "red"
    say("  cashflow after spend + savings " + paint(_money(remainder), style,
                                                        "bold"))
    if snapshot["unbudgeted_average"]:
        warn("average spending outside current envelopes is "
             f"{_money(snapshot['unbudgeted_average'])}/month; it is included "
             "in cashflow but cannot be assigned to an existing envelope")

    header("current budget vs adjusted completed-month averages")
    say(f"  {'category':<34} {'current':>12} {'adjusted avg':>14} {'variance':>12}")
    for row in snapshot["categories"]:
        variance = row["adjusted_average"] - row["current_budget"]
        say(f"  {row['category']:<34} "
            f"{_money(row['current_budget']):>12} "
            f"{_money(row['adjusted_average']):>14} "
            f"{_money(variance):>12}")


def _show_proposal(summary: str, proposal: list[dict], snapshot: dict) -> None:
    header("consultant proposal")
    if summary:
        say(summary)
        say("")
    say(f"  {'category':<30} {'current':>11} {'average':>11} "
        f"{'proposed':>11} {'save/mo':>11} {'save/yr':>11}")
    for row in proposal:
        say(f"  {row['category']:<30} "
            f"{_money(row['current_budget']):>11} "
            f"{_money(row['adjusted_average']):>11} "
            f"{_money(row['proposed_monthly']):>11} "
            f"{_money(row['monthly_savings']):>11} "
            f"{_money(row['annual_savings']):>11}")
        if row["suggestion"]:
            note(f"    suggestion: {row['suggestion']}")
    total = sum(r["proposed_monthly"] for r in proposal)
    monthly = sum(r["monthly_savings"] for r in proposal)
    annual = monthly * 12
    say("")
    say("  proposed envelope total        " + _money(total))
    say("  potential reduction            "
        + paint(f"{_money(monthly)}/month · {_money(annual)}/year",
                "green", "bold"))
    effective_total = total + snapshot["unbudgeted_average"]
    if effective_total > snapshot["spending_ceiling"]:
        warn("proposal plus currently unbudgeted spending is "
             f"{_money(effective_total - snapshot['spending_ceiling'])}/month "
             "over the cashflow spending ceiling")
    else:
        say("  headroom under spending ceiling "
            + _money(snapshot["spending_ceiling"] - effective_total))


def run_consult(cfg, months: int = 6) -> None:
    repo = Path(cfg.repo)
    budget = read_budget(repo)
    if not budget:
        die("no budget.journal envelopes yet — run `budge plan` first")
    if months < 1:
        die("--months must be at least 1")

    banner("budget consultation — past cashflow, practical choices")
    snapshot = build_snapshot(repo, budget, months)
    if not snapshot.get("months"):
        die("no completed-month income or spending data in that window")
    _show_snapshot(snapshot)

    if util.DRY_RUN:
        note("[dry-run] would ask the configured AI for a budget proposal; "
             "no AI call or write was made")
        return

    payload = {"consultation": {
        "completed_months": snapshot["months"],
        "cashflow": {k: snapshot[k] for k in (
            "observed_income", "stated_income", "income_basis",
            "observed_spend", "savings_target", "spending_ceiling",
            "current_budget_total", "unbudgeted_average")},
        "household_goals": snapshot["household_goals"],
        "categories": snapshot["categories"],
    }}
    say("\npreparing a proposal from your aggregate spending patterns...")
    reply = ai.complete(cfg, CONSULT_SYSTEM,
                        json.dumps(payload, ensure_ascii=False))
    parsed = ai.extract_json(reply)
    if not isinstance(parsed, dict):
        warn("the AI returned no usable JSON; showing a completed-month "
             "average baseline instead")
    summary, proposal = validate_proposal(parsed, snapshot)
    _show_proposal(summary, proposal, snapshot)

    new_budget = {row["category"]: row["proposed_monthly"]
                  for row in proposal}
    new_text = _render_budget_text(cfg, new_budget)
    old_path = repo / "budget.journal"
    old_text = old_path.read_text(encoding="utf-8")
    diff = list(difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        "budget.journal (current)", "budget.journal (consult proposal)"))
    header("proposed budget.journal diff")
    if diff:
        for line in diff:
            line = line.rstrip("\n")
            if line.startswith("+") and not line.startswith("+++"):
                say(paint(line, "green"))
            elif line.startswith("-") and not line.startswith("---"):
                say(paint(line, "red"))
            else:
                say(paint(line, "dim"))
    else:
        note("No envelope values would change.")

    accepted = confirm("apply this consultation proposal?", default=False)
    decision_lines = [
        f"window: {snapshot['months'][0]} through {snapshot['months'][-1]}",
        (f"potential reduction: "
         f"{_money(sum(r['monthly_savings'] for r in proposal))}/month, "
         f"{_money(sum(r['annual_savings'] for r in proposal))}/year"),
    ]
    decision_lines += [
        f"{r['category']}: {_money(r['current_budget'])} -> "
        f"{_money(r['proposed_monthly'])} — "
        + (r["suggestion"] or "no specific reduction suggested")
        + (" — ACCEPTED" if accepted else " — declined")
        for r in proposal
    ]
    if accepted:
        write_file(old_path, new_text)
        ok, output = hledger.check(repo / "main.journal")
        if not ok:
            write_file(old_path, old_text)
            die("hledger check rejected the consultation proposal (rolled "
                "back):\n" + output)
        append_decision(repo, "consultation", decision_lines)
        commit_all(repo, "budge consult: budget update")
        say("budget updated and committed")
    else:
        append_decision(repo, "consultation", decision_lines)
        commit_all(repo, "budge consult: proposal declined")
        say("declined — budget.journal untouched; consultation logged")
