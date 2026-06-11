"""Fetch/import pipeline tests: acceptance A2, A3, A4, A6, A12."""

import re

import pytest

from budge import hledger, journal
from budge.fetch import run_fetch
from budge.gitutil import git, head_commit
from budge import util

from conftest import (card_account, checking_account, consistent_balance,
                      invest_account, txn)


def _checking_txns():
    return [
        txn("c1", "2026-05-02", "-43.21", "KROGER"),
        txn("c2", "2026-05-09", "-51.10", "KROGER"),
        txn("c3", "2026-05-15", "-12.50", "STARBUCKS 123"),
        txn("c4", "2026-05-20", "2500.00", "ACME PAYROLL"),
        txn("c5", "2026-06-01", "-200.00", "ONLINE TRANSFER TO CARD"),
    ]


def _card_txns():
    return [
        txn("d1", "2026-05-18", "-80.00", "AMAZON MKTPL"),
        txn("d2", "2026-06-02", "200.00", "PAYMENT THANK YOU"),
    ]


def _serve(simplefin_server, opening_checking=500.0, opening_card=-300.0):
    ck, cd = _checking_txns(), _card_txns()
    simplefin_server.accounts = [
        checking_account(ck, consistent_balance(opening_checking, ck)),
        card_account(cd, consistent_balance(opening_card, cd)),
    ]


def test_backfill_reconciles_A2(env, simplefin_server):
    _serve(simplefin_server)
    run_fetch(env.cfg, backfill_days=90, interactive=False)

    ok, output = hledger.check(env.repo / "main.journal")
    assert ok, output  # opening + txns reconcile via passing assertions

    equity = hledger.account_balance(env.repo / "main.journal",
                                     "equity:opening-balances")
    assert equity  # equity:opening-balances holds the remainder

    # rules-first: transfer-shaped txns went to main cleared, the rest pending
    main = (env.repo / "main.journal").read_text()
    assert "assets:transfers" in main
    pend = journal.parse_pending(env.repo / "pending.journal")
    payees = {e.payee for e in pend}
    assert "KROGER" in payees and "ONLINE TRANSFER TO CARD" not in payees


def test_duplicate_prevention_A3(env, simplefin_server):
    _serve(simplefin_server)
    run_fetch(env.cfg, backfill_days=90, interactive=False)
    ids_once = journal.journal_sf_ids(env.repo / "main.journal") \
        | journal.journal_sf_ids(env.repo / "pending.journal")

    for _ in range(3):  # re-run N times, including a re-backfill
        run_fetch(env.cfg)
        run_fetch(env.cfg, backfill_days=90, interactive=False)

    main = (env.repo / "main.journal").read_text()
    pend = (env.repo / "pending.journal").read_text()
    for tid in ids_once:
        assert main.count(f"simplefin_id:{tid}") \
            + main.count(f"simplefin_id: {tid}") \
            + pend.count(f"simplefin_id: {tid}") == 1, tid
    ok, output = hledger.check(env.repo / "main.journal")
    assert ok, output


def test_card_payment_clears_A4(env, simplefin_server):
    _serve(simplefin_server)
    run_fetch(env.cfg, backfill_days=90, interactive=False)

    # both feeds posted through the clearing account, which nets to zero
    bal = hledger.account_balance(env.repo / "main.journal",
                                  journal.TRANSFERS)
    assert re.fullmatch(r"\$?0(\.0+)?|0", bal.replace(",", "")), bal
    # and no expense was recorded for either side
    main = (env.repo / "main.journal").read_text()
    for line in main.splitlines():
        if "PAYMENT THANK YOU" in line or "ONLINE TRANSFER" in line:
            assert "expenses:" not in line


def test_pending_visibility_A6(env, simplefin_server, fake_ai):
    """An unreviewed AI-categorized txn draws down its envelope immediately."""
    from budge.categorize import run_categorize

    _serve(simplefin_server)
    run_fetch(env.cfg, backfill_days=90, interactive=False)

    fake_ai.respond({"map": {
        "KROGER": {"category": "expenses:groceries", "confidence": "high"},
        "STARBUCKS 123": {"category": "expenses:dining",
                          "confidence": "medium"},
        "AMAZON MKTPL": {"category": "expenses:shopping",
                         "confidence": "medium"},
        "ACME PAYROLL": {"category": "income:salary", "confidence": "high"},
    }})
    for cat in ("expenses:groceries", "expenses:dining", "expenses:shopping",
                "income:salary"):
        from budge.scaffold import declare_account
        declare_account(env.repo, cat)
    run_categorize(env.cfg)

    # nothing promoted, yet the budget already sees the spend (status !)
    bal = hledger.account_balance(env.repo / "main.journal",
                                  "expenses:groceries")
    assert "94.31" in bal  # 43.21 + 51.10, drawn down at the AI's best guess
    assert journal.parse_pending(env.repo / "pending.journal")


def test_dry_run_writes_nothing_A12(env, simplefin_server):
    _serve(simplefin_server)
    before_main = (env.repo / "main.journal").read_text()
    before_head = head_commit(env.repo)
    util.DRY_RUN = True
    try:
        run_fetch(env.cfg, backfill_days=90, interactive=False)
    finally:
        util.DRY_RUN = False
    assert (env.repo / "main.journal").read_text() == before_main
    assert head_commit(env.repo) == before_head
    assert git(env.repo, "status", "--porcelain").stdout.strip() == ""
    assert not list((env.repo / "import" / "raw").glob("*/*.csv"))


def test_zero_transaction_account_gets_opening_balance(env, simplefin_server):
    """A backfilled account with no transactions in the window (e.g. a stock
    plan) still reconciles: opening balance = reported balance."""
    simplefin_server.accounts = [invest_account("39311.40")]
    run_fetch(env.cfg, backfill_days=90, interactive=False)
    ok, out = hledger.check(env.repo / "main.journal")
    assert ok, out
    bal = hledger.account_balance(env.repo / "main.journal",
                                  "assets:stock-plan")
    assert "39311.40" in bal.replace(",", "")


def test_drift_account_absorbs_market_moves(env, simplefin_server):
    """Investment value changes without transactions must not fail the daily
    assertion — they post as unrealized gains/losses."""
    simplefin_server.accounts = [invest_account("39311.40")]
    run_fetch(env.cfg, backfill_days=90, interactive=False)

    simplefin_server.accounts = [invest_account("39851.15")]  # market up
    run_fetch(env.cfg)
    ok, out = hledger.check(env.repo / "main.journal")
    assert ok, out
    main = (env.repo / "main.journal").read_text()
    assert "market value adjustment" in main
    gains = hledger.account_balance(env.repo / "main.journal",
                                    "equity:unrealized-gains")
    assert "539.75" in gains.replace(",", "")

    simplefin_server.accounts = [invest_account("39000.00")]  # market down
    run_fetch(env.cfg)
    ok, out = hledger.check(env.repo / "main.journal")
    assert ok, out


def test_backfill_rerun_does_not_duplicate_opening(env, simplefin_server):
    simplefin_server.accounts = [invest_account("39311.40")]
    run_fetch(env.cfg, backfill_days=90, interactive=False)
    run_fetch(env.cfg, backfill_days=90, interactive=False)  # re-run
    main = (env.repo / "main.journal").read_text()
    assert main.count("* opening balances") == 1
    ok, out = hledger.check(env.repo / "main.journal")
    assert ok, out


def test_informational_simplefin_message_is_not_fatal(env, simplefin_server,
                                                      capsys):
    """SimpleFIN's 'range was capped' notice must not kill the backfill."""
    _serve(simplefin_server)
    simplefin_server.messages = [
        "Requested date range exceeds limit of 90 days and was capped."]
    run_fetch(env.cfg, backfill_days=90, interactive=False)
    err = capsys.readouterr().err
    assert "capped" in err                      # surfaced as a warning
    ok, out = hledger.check(env.repo / "main.journal")
    assert ok, out                              # and the import completed


def test_no_accounts_plus_errors_is_fatal(env, simplefin_server):
    simplefin_server.accounts = []
    simplefin_server.messages = ["Connection to TestBank needs attention"]
    from budge.simplefin import SimpleFINError
    with pytest.raises(SimpleFINError, match="needs attention"):
        run_fetch(env.cfg, backfill_days=90, interactive=False)


def test_assertion_failure_is_pipeline_failure(env, simplefin_server):
    """Continuous reconciliation: a drifted reported balance fails the run.

    (The backfill reconciles by construction — opening = reported − txns —
    so the drift has to appear on a subsequent daily fetch.)
    """
    ck = _checking_txns()
    simplefin_server.accounts = [
        checking_account(ck, consistent_balance(500.0, ck))]
    run_fetch(env.cfg, backfill_days=90, interactive=False)

    new = txn("c9", "2026-06-09", "-10.00", "KROGER")
    ck2 = ck + [new]
    simplefin_server.accounts = [
        checking_account(ck2, consistent_balance(500.0, ck2) + 99)  # drift
    ]
    with pytest.raises(SystemExit):
        run_fetch(env.cfg)
