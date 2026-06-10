"""Budget wizard tests: acceptance A11 (skip), A13, A14, A15."""

import datetime as dt

from budge import hledger
from budge.fetch import run_fetch
from budge.plan import read_budget, read_household, run_plan

from conftest import checking_account, consistent_balance, txn

PLAN_REPLY = {
    "plan": {
        "categories": [
            {"account": "expenses:groceries", "note": "supermarkets"},
            {"account": "expenses:dining", "note": "coffee & restaurants"},
            {"account": "income:salary", "note": "payroll"},
        ],
        "rules": [
            {"payee": "KROGER", "account": "expenses:groceries"},
            {"payee": "BLUE BOTTLE", "account": "expenses:dining"},
            {"payee": "ACME PAYROLL", "account": "income:salary"},
        ],
    }
}


def _backfill(env, simplefin_server):
    txns = [
        txn("w1", "2026-04-05", "-120.00", "KROGER"),
        txn("w2", "2026-05-05", "-130.00", "KROGER"),
        txn("w3", "2026-05-12", "-5.00", "BLUE BOTTLE"),
        txn("w4", "2026-05-30", "3000.00", "ACME PAYROLL"),
    ]
    simplefin_server.accounts = [
        checking_account(txns, consistent_balance(800.0, txns))]
    run_fetch(env.cfg, backfill_days=90, interactive=False)


def test_bootstrap_A13(env, simplefin_server, fake_ai, answers, capsys):
    _backfill(env, simplefin_server)
    fake_ai.respond(PLAN_REPLY)
    answers.extend([
        "6000",          # 1/3 income
        "1000",          # 2/3 savings target
        "saving for a car; childcare starts in the fall",  # 3/3 goals
        "y",             # accept chart of accounts
        "y",             # set envelopes now
        "200",           # expenses:dining target
        "600",           # expenses:groceries target
        "150",           # sinking fund
        "y",             # write starter rules
    ])
    run_plan(env.cfg, mode="bootstrap")

    out = capsys.readouterr().out
    assert "90 days" in out                      # sample-bias disclosure
    assert "sinking-fund" in out or "sinking" in out

    # three confirmable artifacts, all written, hledger check passing
    accounts = (env.repo / "accounts.journal").read_text()
    assert "account expenses:groceries" in accounts
    budget = read_budget(env.repo)
    assert budget["expenses:groceries"] == 600.0
    assert budget["expenses:sinking-fund"] == 150.0
    assert sum(budget.values()) <= 6000 - 1000   # envelopes within ceiling
    rules = (env.repo / "import/rules/checking.rules").read_text()
    assert "KROGER" in rules and "expenses:groceries" in rules
    ok, output = hledger.check(env.repo / "main.journal")
    assert ok, output

    # the starter rules shrank the day-one review pile
    from budge import journal
    pend = journal.parse_pending(env.repo / "pending.journal")
    assert not [e for e in pend if e.payee == "KROGER"]

    # household.md holds intake + the run's decisions
    hh = (env.repo / "household.md").read_text()
    assert "6000" in hh and "childcare" in hh and "Decision log" in hh


def test_over_ceiling_targets_A14(env, simplefin_server, fake_ai, answers,
                                  capsys):
    _backfill(env, simplefin_server)
    fake_ai.respond(PLAN_REPLY)
    answers.extend([
        "6000", "1000", "no goals",
        "y",             # accept chart
        "y",             # set envelopes
        "3000",          # dining (way over)
        "4000",          # groceries (way over: total 7000 > 5000 ceiling)
        "",              # no sinking fund
        "n",             # do NOT re-enter — keep over-ceiling targets
        "n",             # skip writing rules
    ])
    run_plan(env.cfg, mode="bootstrap")
    out = capsys.readouterr().out
    assert "OVER the ceiling" in out             # gap surfaced
    assert "discretionary slack" in out          # slack candidates named
    assert "won't pick the cuts" in out          # never auto-resolves
    budget = read_budget(env.repo)
    assert budget["expenses:dining"] == 3000.0   # not silently rescaled


def test_wizard_skips_when_budget_exists_A11(env, capsys):
    (env.repo / "budget.journal").write_text(
        "~ monthly\n    expenses:groceries  $500.00\n    assets:checking\n")
    run_plan(env.cfg, from_setup=True)  # no `answers` fixture: any prompt
    out = capsys.readouterr().out       # would raise EOFError and fail
    assert "skipped" in out


def test_reassessment_A15(env, fake_ai, answers, capsys):
    # books with 3 months of categorized dining, consistently over budget
    today = dt.date.today()
    entries = []
    for i in (1, 2, 3):
        month = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        for _ in range(i):
            pass
        m = today.month - i
        y = today.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        entries.append(
            f"{y}-{m:02d}-15 * RESTAURANT\n"
            f"    expenses:dining     $150.00\n"
            f"    assets:checking    $-150.00\n"
        )
    with open(env.repo / "main.journal", "a") as f:
        f.write("\n" + "\n".join(entries))
    (env.repo / "accounts.journal").write_text(
        "account assets:checking\naccount assets:transfers\n"
        "account equity:opening-balances\naccount expenses:uncategorized\n"
        "account expenses:dining\n")
    (env.repo / "budget.journal").write_text(
        "~ monthly\n    expenses:dining  $100.00\n    assets:checking\n")
    budget_before = (env.repo / "budget.journal").read_text()

    answers.extend([
        "6000", "1000", "same goals",
        "n",   # decline the proposed diff
    ])
    run_plan(env.cfg, mode="reassess", months=3)

    out = capsys.readouterr().out
    assert "consecutive months" in out                  # streak reported
    assert "proposed budget.journal diff" in out
    # declining leaves the repo untouched...
    assert (env.repo / "budget.journal").read_text() == budget_before
    # ...and household.md records the run's decisions
    hh = (env.repo / "household.md").read_text()
    assert "declined" in hh


def test_dormancy_reported(env, fake_ai, answers, capsys):
    (env.repo / "accounts.journal").write_text(
        "account assets:checking\naccount assets:transfers\n"
        "account equity:opening-balances\naccount expenses:uncategorized\n"
        "account expenses:gym\n")
    (env.repo / "budget.journal").write_text(
        "~ monthly\n    expenses:gym  $50.00\n    assets:checking\n")
    answers.extend(["6000", "1000", "goals"])
    run_plan(env.cfg, mode="reassess", months=3)
    out = capsys.readouterr().out
    assert "DORMANT" in out
