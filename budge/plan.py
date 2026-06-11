"""Budget planning wizard — `budge plan` (PRD section 7.8).

Core stance: actuals are descriptive, budgets are prescriptive. The wizard
organizes the user's own data against the user's own stated constraints and
proposes; the human decides. It never writes any file without explicit
confirmation, never cites external benchmarks, and never auto-resolves
conflicts between goals.
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import io
import json
import re
from pathlib import Path

from . import ai, categorize, fetch, hledger, journal
from .gitutil import commit_all
from .scaffold import (add_vendor_rule, declare_account, load_accounts,
                       payee_pattern)
from .util import (append_file, confirm, die, edit_text, prompt, say, warn,
                   write_file)

NEEDS_HINTS = ("housing", "rent", "mortgage", "utilities", "insurance",
               "debt", "childcare", "medical", "health")

WIZARD_SYSTEM = """\
You are helping organize a household's own bank transactions into a chart of
accounts for plain-text accounting (hledger). You will receive merchant
aggregates (payee, transaction count, average monthly amount) plus the
household's stated goals. Cluster the merchants into sensible categories.

Hard rules:
- Use ONLY the data given. No external benchmarks, no "typical household"
  claims, no advice. You are a bookkeeping assistant, not a financial advisor.
- Account names: lowercase, colon-separated, starting with `expenses:` or
  `income:` (e.g. expenses:groceries, income:salary). 8-20 expense categories.
- Map each significant merchant (2+ transactions) to exactly one category.

Reply with ONLY a JSON object:
{
  "categories": [{"account": "expenses:groceries", "note": "supermarkets"}],
  "rules": [{"payee": "KROGER", "account": "expenses:groceries"}]
}
"""

SAMPLE_BIAS_NOTE = """\
NOTE on the data you are about to see: it covers roughly 90 days. Annual and
seasonal expenses — insurance premiums, vehicle registration, holidays — are
mostly invisible in a single quarter. Rather than extrapolating this quarter
as the whole year, consider a sinking-fund envelope (e.g.
expenses:sinking-fund) that accumulates monthly for those irregulars.
"""


# ------------------------------------------------------------ household ----

def _household_path(repo: Path) -> Path:
    return Path(repo) / "household.md"


def read_household(repo: Path) -> dict:
    text = _household_path(repo).read_text(encoding="utf-8") \
        if _household_path(repo).exists() else ""
    income = savings = 0.0
    m = re.search(r"take-home income:\s*\$?([\d,.]+)", text)
    if m:
        income = float(m.group(1).replace(",", ""))
    m = re.search(r"savings target:\s*\$?([\d,.]+)", text)
    if m:
        savings = float(m.group(1).replace(",", ""))
    goals = ""
    m = re.search(r"## Goals & upcoming changes\n+(.*?)(\n## |\Z)", text,
                  re.DOTALL)
    if m:
        goals = m.group(1).strip()
    return {"income": income, "savings": savings, "goals": goals,
            "text": text}


def intake(repo: Path) -> dict:
    """Exactly three prompts (PRD 7.8), stored in household.md."""
    current = read_household(repo)
    say("\n— Intake (three questions; answers live in household.md) —")
    income = float(prompt(
        "1/3  Monthly take-home income",
        str(current["income"] or "")) or 0)
    savings = float(prompt(
        "2/3  Monthly savings target",
        str(current["savings"] or "")) or 0)
    goals = prompt(
        "3/3  What are you saving for, and what's changing in the next year?",
        current["goals"])
    data = {"income": income, "savings": savings, "goals": goals}
    _write_household_header(repo, data, current["text"])
    return data


def _write_household_header(repo: Path, data: dict, old_text: str) -> None:
    decisions = ""
    m = re.search(r"(## Decision log.*)", old_text, re.DOTALL)
    if m:
        decisions = m.group(1)
    text = (
        "# household.md\n\n"
        f"- Monthly take-home income: ${data['income']:.2f}\n"
        f"- Monthly savings target: ${data['savings']:.2f}\n\n"
        "## Goals & upcoming changes\n\n"
        f"{data['goals']}\n\n"
        + (decisions or "## Decision log\n")
    )
    write_file(_household_path(repo), text)


def append_decision(repo: Path, mode: str, lines: list) -> None:
    """Accumulate the 'why' behind budget history (PRD 7.8)."""
    text = f"\n### {dt.date.today().isoformat()} — {mode}\n\n"
    text += "".join(f"- {line}\n" for line in lines)
    append_file(_household_path(repo), text)


# ------------------------------------------------------------ aggregates ---

def merchant_aggregates(repo: Path) -> dict:
    """payee -> {count, total, monthly_avg} from the raw CSVs (spend < 0)."""
    rows = fetch.all_raw_rows(repo).values()
    dates = [r["date"] for _, r in rows] or [dt.date.today().isoformat()]
    span_days = max(
        (dt.date.fromisoformat(max(dates))
         - dt.date.fromisoformat(min(dates))).days, 1)
    months = max(span_days / 30.44, 1.0)
    agg = {}
    for _, row in rows:
        amt = float(row["amount"])
        a = agg.setdefault(row["payee"], {"count": 0, "total": 0.0})
        a["count"] += 1
        a["total"] += amt
    for payee, a in agg.items():
        a["monthly_avg"] = round(a["total"] / months, 2)
        a["total"] = round(a["total"], 2)
    return agg


# ------------------------------------------------------------- bootstrap ---

def bootstrap(cfg, household: dict) -> None:
    repo = cfg.repo
    say(f"\n{SAMPLE_BIAS_NOTE}")
    agg = merchant_aggregates(repo)
    if not agg:
        die("no transactions found — run the backfill first (budge setup)")

    # AI clustering pass — payees and aggregate amounts only (no account
    # numbers, no balances, no full history: same minimization stance as 6).
    all_merchants = [
        {"payee": p, "count": a["count"], "monthly_avg": a["monthly_avg"]}
        for p, a in sorted(agg.items(), key=lambda kv: kv[1]["count"],
                           reverse=True)
        if a["count"] >= 1
    ]
    limit = 150
    parsed = {}
    while True:
        merchants = all_merchants[:limit]
        user = json.dumps({
            "monthly_take_home": household["income"],
            "monthly_savings_target": household["savings"],
            "context": household["goals"],
            "merchants": merchants,
        }, ensure_ascii=False)
        say(f"analyzing your transactions ({len(merchants)} merchants; this "
            "can take a few minutes)...")
        try:
            reply = ai.complete(cfg, WIZARD_SYSTEM, user)
            parsed = ai.extract_json(reply) or {}
            break
        except ai.AIError as e:
            warn(f"the AI analysis failed: {e}")
            from .util import choose
            action = choose("What now?", [
                ("retry", "Try again"),
                ("fewer", "Try again with fewer merchants (smaller, faster "
                          "request)"),
                ("skip", "Continue without AI (generic starter chart; you "
                         "can re-run `budge plan --bootstrap` later)"),
            ])
            if action == "skip":
                break
            if action == "fewer":
                limit = max(40, limit // 2)
    categories = [
        c["account"].strip() for c in parsed.get("categories", [])
        if isinstance(c, dict)
        and re.fullmatch(r"(expenses|income):[a-z0-9:_-]+",
                         str(c.get("account", "")).strip())
    ]
    rules = [
        (str(r["payee"]).strip(), str(r["account"]).strip())
        for r in parsed.get("rules", [])
        if isinstance(r, dict) and r.get("payee")
        and str(r.get("account", "")).strip() in categories
    ]
    if not categories:
        categories = ["expenses:groceries", "expenses:dining",
                      "expenses:transport", "expenses:utilities",
                      "expenses:household", "expenses:entertainment",
                      "expenses:health", "income:salary"]
        say("(using a generic starter chart — the AI pass returned nothing)")

    # ---- artifact (a): chart of accounts -------------------------------
    say("\n— Proposed chart of accounts (artifact 1/3) —")
    proposal = "\n".join(sorted(set(categories)))
    while True:
        say(proposal + "\n")
        act = prompt("[y] accept  [e] edit in $EDITOR  [n] skip", "y").lower()
        if act == "e":
            proposal = "\n".join(
                l.strip() for l in edit_text(proposal).splitlines()
                if l.strip())
            continue
        break
    wrote_accounts = False
    if act != "n":
        for account in proposal.splitlines():
            declare_account(repo, account)
        wrote_accounts = True
        say("accounts.journal updated")
    chart = proposal.splitlines() if act != "n" else sorted(set(categories))

    # ---- artifact (b): envelope amounts --------------------------------
    say("\n— Envelope amounts (artifact 2/3) —")
    ceiling = household["income"] - household["savings"]
    say(f"income ${household['income']:.2f} − savings target "
        f"${household['savings']:.2f} = spending ceiling ${ceiling:.2f}/mo")
    observed = {c: 0.0 for c in chart if c.startswith("expenses:")}
    payee_cat = dict(rules)
    for payee, a in agg.items():
        cat = payee_cat.get(payee)
        if cat in observed and a["monthly_avg"] < 0:
            observed[cat] += -a["monthly_avg"]
    envelopes = {}
    if confirm("Set envelope amounts now? (observed monthly actuals are the "
               "defaults; you type the targets)", default=True):
        while True:
            total = 0.0
            for cat in sorted(observed):
                default = f"{observed[cat]:.2f}"
                value = prompt(f"  {cat} (observed ~${default})", default)
                try:
                    envelopes[cat] = float(value.replace("$", "")
                                           .replace(",", ""))
                except ValueError:
                    envelopes[cat] = observed[cat]
                total += envelopes[cat]
                say(f"    running total ${total:.2f} of ${ceiling:.2f}")
            extra = prompt("sinking fund for annual/seasonal irregulars "
                           "(monthly amount, blank to skip)", "")
            if extra.strip():
                envelopes["expenses:sinking-fund"] = float(extra)
                declare_account(repo, "expenses:sinking-fund")
                total += envelopes["expenses:sinking-fund"]
            if total <= ceiling:
                say(f"targets total ${total:.2f} — within the ceiling "
                    f"(${ceiling - total:.2f} unallocated)")
                break
            # A14: surface the gap and slack candidates; never auto-resolve.
            gap = total - ceiling
            slack = sorted(
                (c for c in envelopes
                 if not any(h in c for h in NEEDS_HINTS)),
                key=lambda c: -envelopes[c])[:5]
            say(f"\ntargets total ${total:.2f} — ${gap:.2f} OVER the "
                f"ceiling.")
            say("largest envelopes with discretionary slack: "
                + ", ".join(f"{c} (${envelopes[c]:.2f})" for c in slack))
            say("budge won't pick the cuts — that's a household decision.")
            if not confirm("re-enter the amounts?", default=True):
                warn("keeping over-ceiling targets at your request")
                break
        _write_budget(cfg, envelopes)
    wrote_budget = bool(envelopes)

    # ---- artifact (c): starter merchant rules --------------------------
    say("\n— Starter merchant rules (artifact 3/3) —")
    rules = rules[:30]
    wrote_rules = 0
    if rules:
        for payee, cat in rules:
            say(f"  {payee!r:<40} -> {cat}")
        if confirm(f"Write these {len(rules)} rules? (they shrink the "
                   "day-one review pile immediately)", default=True):
            slug_by_payee = {}
            for sf_id, (slug, row) in fetch.all_raw_rows(repo).items():
                slug_by_payee.setdefault(row["payee"], set()).add(slug)
            for payee, cat in rules:
                declare_account(repo, cat)
                pattern = payee_pattern(payee)
                for slug in slug_by_payee.get(
                        payee, {a["slug"] for a in load_accounts(repo)}):
                    add_vendor_rule(repo, slug, pattern, cat)
                wrote_rules += 1
            result = categorize.regenerate(cfg)
            say(f"rules absorbed {result['promoted_by_rule']} backfilled "
                f"txns; {result['kept']} remain for review")

    ok, output = hledger.check(repo / "main.journal")
    if not ok:
        die("hledger check failed after wizard writes:\n" + output)
    append_decision(repo, "bootstrap", [
        f"income ${household['income']:.2f}, savings target "
        f"${household['savings']:.2f}, ceiling ${ceiling:.2f}",
        f"chart of accounts: {len(chart)} categories "
        f"({'written' if wrote_accounts else 'skipped'})",
        *(f"{c}: observed ${observed.get(c, 0):.2f} -> chosen ${v:.2f}"
          for c, v in sorted(envelopes.items())),
        f"starter rules written: {wrote_rules}",
    ])
    commit_all(repo, "budge plan: bootstrap budget")
    say("\nwizard done — view the budget with: "
        "hledger -f main.journal balance --budget -M expenses")


def _write_budget(cfg, envelopes: dict) -> None:
    write_file(cfg.repo / "budget.journal",
               _render_budget_text(cfg, envelopes))


# ----------------------------------------------------------- re-assess -----

def read_budget(repo: Path) -> dict:
    """Parse the envelopes back out of budget.journal (we wrote it)."""
    path = Path(repo) / "budget.journal"
    if not path.exists():
        return {}
    envelopes, in_txn = {}, False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("~"):
            in_txn = True
            continue
        if in_txn:
            m = re.match(r"^\s+(\S[\S ]*?)\s{2,}\$([\d,.]+)\s*$", line)
            if m:
                envelopes[m.group(1).strip()] = float(
                    m.group(2).replace(",", ""))
    return envelopes


def monthly_actuals(repo: Path, months: int) -> dict:
    """category -> [spend per month] via `hledger balance -M -O csv`."""
    end = dt.date.today().replace(day=1)
    start = (end - dt.timedelta(days=months * 31)).replace(day=1)
    proc = hledger.hledger([
        "balance", "-f", repo / "main.journal", "expenses", "-M",
        "-b", start.isoformat(), "-e", end.isoformat(),
        "--no-total", "--flat", "-O", "csv",
    ], check=False)
    out = {}
    if proc.returncode != 0:
        return out
    reader = csv.reader(io.StringIO(proc.stdout))
    header = next(reader, None)
    for row in reader:
        if not row or row[0] in ("total", "Total:", ""):
            continue
        vals = []
        for cell in row[1:]:
            m = re.search(r"-?[\d,]+\.?\d*", cell.replace("$", ""))
            vals.append(float(m.group(0).replace(",", "")) if m else 0.0)
        out[row[0]] = vals
    return out


def reassess(cfg, months: int = 3) -> None:
    repo = cfg.repo
    budget = read_budget(repo)
    if not budget:
        die("no budget.journal envelopes yet — run `budge plan` bootstrap "
            "first")
    household = read_household(repo)

    # The operator-facing report IS hledger's own (stock interface):
    say("\nhledger balance --budget over the trailing window:\n")
    proc = hledger.hledger([
        "balance", "-f", repo / "main.journal", "--budget", "-M",
        "expenses",
        "-b", (dt.date.today() - dt.timedelta(days=months * 31)).isoformat(),
    ], check=False)
    say(proc.stdout or proc.stderr)

    actuals = monthly_actuals(repo, months)
    report, proposals = [], {}
    for cat, amount in sorted(budget.items()):
        series = actuals.get(cat, [])
        if not series:
            report.append(f"{cat}: DORMANT — no transactions in the window "
                          f"(budgeted ${amount:.2f}/mo)")
            continue
        avg = sum(series) / len(series)
        over = [s > amount * 1.02 for s in series]
        streak = 0
        for flag in reversed(over):
            if flag:
                streak += 1
            else:
                break
        if streak >= 2:
            pct = (avg / amount - 1) * 100 if amount else 0
            report.append(
                f"{cat}: {pct:.0f}% over for {streak} consecutive months "
                f"(avg ${avg:.2f} vs ${amount:.2f})")
            proposals[cat] = round(avg / 10) * 10
        elif avg > amount:
            report.append(f"{cat}: over on average (${avg:.2f} vs "
                          f"${amount:.2f}), not a streak")

    say("\n— variance —")
    for line in report or ["all envelopes on track"]:
        say("  " + line)

    # household.md context vs actuals (arithmetic only, stated plainly)
    spend_total = sum(sum(s) / len(s) for s in actuals.values() if s)
    if household["income"]:
        implied = household["income"] - spend_total
        if implied < household["savings"]:
            say(f"\ncontext check: average spend ${spend_total:.2f}/mo "
                f"implies ${implied:.2f}/mo saved vs your stated target "
                f"${household['savings']:.2f} — the savings target is no "
                "longer arithmetic-compatible. Revisit?")

    if not proposals:
        append_decision(repo, "re-assessment",
                        ["no changes proposed"] + report)
        say("\nno budget changes proposed")
        return

    new_budget = dict(budget)
    new_budget.update(proposals)
    old_text = (repo / "budget.journal").read_text(encoding="utf-8")
    _write_budget_text = _render_budget_text(cfg, new_budget)
    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        _write_budget_text.splitlines(keepends=True),
        "budget.journal (current)", "budget.journal (proposed)"))
    say("\n— proposed budget.journal diff —\n" + diff)
    decisions = [f"{c}: ${budget[c]:.2f} -> proposed ${v:.2f}"
                 for c, v in proposals.items()]
    if confirm("apply this diff?", default=False):
        write_file(repo / "budget.journal", _write_budget_text)
        ok, output = hledger.check(repo / "main.journal")
        if not ok:
            (repo / "budget.journal").write_text(old_text, encoding="utf-8")
            die("hledger check rejected the new budget (rolled back):\n"
                + output)
        append_decision(repo, "re-assessment",
                        [d + " — ACCEPTED" for d in decisions])
        commit_all(repo, "budge plan: re-assessment budget update")
        say("budget updated and committed")
    else:
        append_decision(repo, "re-assessment",
                        [d + " — declined" for d in decisions])
        commit_all(repo, "budge plan: re-assessment (declined, decisions "
                         "logged)")
        say("declined — budget.journal untouched")


def _render_budget_text(cfg, envelopes: dict) -> str:
    accounts = load_accounts(cfg.repo)
    balancing = accounts[0]["account"] if accounts else "assets:checking"
    lines = [
        "; budget.journal — monthly envelopes "
        f"(written by budge plan, {dt.date.today().isoformat()})",
        "; view: hledger -f main.journal balance --budget -M expenses",
        "",
        "~ monthly",
    ]
    for cat, amount in sorted(envelopes.items()):
        lines.append(f"    {cat:<40s}  ${amount:.2f}")
    lines.append(f"    {balancing}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- entry ---

def run_plan(cfg, mode: str = None, months: int = 3,
             from_setup: bool = False) -> None:
    repo = cfg.repo
    has_budget = bool(read_budget(repo))
    if from_setup and has_budget:
        say("budget.journal already has envelopes — wizard skipped "
            "(run `budge plan` to re-assess)")  # acceptance A11
        return
    if mode is None:
        mode = "reassess" if has_budget else "bootstrap"
    household = intake(repo)
    if mode == "bootstrap":
        bootstrap(cfg, household)
    else:
        reassess(cfg, months=months)
