"""Cashflow-aware budget consultation tests."""

import datetime as dt

from budge import hledger, util
from budge.consult import build_snapshot, run_consult, validate_proposal
from budge.plan import read_budget


def _month_before(today, count):
    serial = today.year * 12 + today.month - 1 - count
    return serial // 12, serial % 12 + 1


def _seed_history(env):
    entries = []
    for offset in (1, 2, 3):
        year, month = _month_before(dt.date.today(), offset)
        entries += [
            (f"{year}-{month:02d}-01 * ACME PAYROLL\n"
             "    assets:checking          $3000.00\n"
             "    income:salary\n"),
            (f"{year}-{month:02d}-10 * CORNER MARKET\n"
             "    expenses:groceries       $600.00\n"
             "    assets:checking         $-600.00\n"),
            (f"{year}-{month:02d}-15 * CAFE LOCAL\n"
             "    expenses:dining          $300.00\n"
             "    assets:checking         $-300.00\n"),
        ]
    with open(env.repo / "main.journal", "a", encoding="utf-8") as f:
        f.write("\n" + "\n".join(entries))
    (env.repo / "accounts.journal").write_text(
        "account assets:checking\n"
        "account assets:transfers\n"
        "account equity:opening-balances\n"
        "account expenses:uncategorized\n"
        "account expenses:dining\n"
        "account expenses:groceries\n"
        "account income:salary\n",
        encoding="utf-8",
    )
    (env.repo / "budget.journal").write_text(
        "~ monthly\n"
        "    expenses:dining       $400.00\n"
        "    expenses:groceries    $650.00\n"
        "    assets:checking\n",
        encoding="utf-8",
    )
    (env.repo / "household.md").write_text(
        "# household.md\n\n"
        "- Monthly take-home income: $3000.00\n"
        "- Monthly savings target: $500.00\n\n"
        "## Goals & upcoming changes\n\nBuild an emergency fund.\n\n"
        "## Decision log\n",
        encoding="utf-8",
    )


def _proposal(fake_ai):
    fake_ai.respond({"consult": {
        "summary": "Dining is the clearest practical reduction.",
        "categories": [
            {"category": "expenses:dining", "proposed_monthly": 250,
             "suggestion": "Set a weekly CAFE LOCAL limit."},
            {"category": "expenses:groceries", "proposed_monthly": 600,
             "suggestion": "Keep the current grocery pace."},
        ],
    }})


def test_consult_snapshot_uses_completed_months_and_merchants(env):
    _seed_history(env)
    snapshot = build_snapshot(env.repo, read_budget(env.repo), months=6)

    assert len(snapshot["months"]) == 3
    assert snapshot["months"][-1] != dt.date.today().strftime("%Y-%m")
    assert snapshot["observed_income"] == 3000.0
    assert snapshot["observed_spend"] == 900.0
    dining = next(c for c in snapshot["categories"]
                  if c["category"] == "expenses:dining")
    assert dining["adjusted_average"] == 300.0
    assert dining["top_merchants"] == [
        {"payee": "CAFE LOCAL", "monthly_average": 300.0}]


def test_proposal_savings_are_computed_not_trusted_from_ai(env):
    _seed_history(env)
    snapshot = build_snapshot(env.repo, read_budget(env.repo), months=6)
    _, proposal = validate_proposal({"categories": [{
        "category": "expenses:dining",
        "proposed_monthly": 250,
        "monthly_savings": 999999,
        "annual_savings": 1,
    }]}, snapshot)
    dining = next(r for r in proposal
                  if r["category"] == "expenses:dining")
    assert dining["monthly_savings"] == 50.0
    assert dining["annual_savings"] == 600.0


def test_consult_decline_shows_monthly_and_annual_savings(
        env, fake_ai, answers, capsys):
    _seed_history(env)
    _proposal(fake_ai)
    before = (env.repo / "budget.journal").read_text(encoding="utf-8")
    answers.append("n")

    run_consult(env.cfg, months=6)

    out = capsys.readouterr().out
    assert "current budget vs adjusted" in out
    assert "$50.00/month" in out
    assert "$600.00/year" in out
    assert "Set a weekly CAFE LOCAL limit" in out
    assert (env.repo / "budget.journal").read_text(encoding="utf-8") == before
    assert "declined" in (env.repo / "household.md").read_text(
        encoding="utf-8")


def test_consult_accepts_proposal_behind_hledger_gate(
        env, fake_ai, answers):
    _seed_history(env)
    _proposal(fake_ai)
    answers.append("y")

    run_consult(env.cfg, months=6)

    budget = read_budget(env.repo)
    assert budget == {"expenses:dining": 250.0,
                      "expenses:groceries": 600.0}
    ok, output = hledger.check(env.repo / "main.journal")
    assert ok, output
    assert "ACCEPTED" in (env.repo / "household.md").read_text(
        encoding="utf-8")


def test_consult_dry_run_never_calls_ai(env, monkeypatch, capsys):
    _seed_history(env)

    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not call the AI")

    monkeypatch.setattr("budge.consult.ai.complete", forbidden)
    util.DRY_RUN = True
    try:
        run_consult(env.cfg, months=6)
    finally:
        util.DRY_RUN = False
    assert "no AI call or write" in capsys.readouterr().out
