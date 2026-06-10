"""Categorizer tests: acceptance A5, A9; data minimization; audit log."""

import json

from budge import ai, ailog, journal
from budge.categorize import run_categorize
from budge.fetch import run_fetch
from budge.scaffold import declare_account

from conftest import checking_account, consistent_balance, txn


def _import_some(env, simplefin_server, txns):
    simplefin_server.accounts = [
        checking_account(txns, consistent_balance(500.0, txns))]
    run_fetch(env.cfg, backfill_days=90, interactive=False)


def test_transfer_reaches_ai_A5(env, simplefin_server, fake_ai):
    """A transfer the rules missed: model flags it, posts to transfers."""
    txns = [txn("x1", "2026-06-01", "-150.00", "MOVE MONEY OUT")]
    _import_some(env, simplefin_server, txns)
    fake_ai.respond({"map": {
        "MOVE MONEY OUT": {"category": "", "transfer": True,
                           "confidence": "low",
                           "rationale": "looks like an own-account move"},
    }})
    run_categorize(env.cfg)
    pend = journal.parse_pending(env.repo / "pending.journal")
    assert pend[0].category == journal.TRANSFERS  # surfaced in review as gap


def test_malformed_ai_output_rejected_A9(env, simplefin_server, fake_ai):
    txns = [txn("x2", "2026-06-01", "-9.99", "MYSTERY VENDOR")]
    _import_some(env, simplefin_server, txns)
    fake_ai.respond({"malformed": True})
    run_categorize(env.cfg)

    pend = journal.parse_pending(env.repo / "pending.journal")
    assert pend[0].category == journal.UNCATEGORIZED  # never guessed at
    events = ailog.read_all(env.repo)
    assert any(e["event"] == "reject" for e in events)  # and it was logged


def test_bad_category_rejected(env, simplefin_server, fake_ai):
    """Category outside the chart of accounts fails the output contract."""
    txns = [txn("x3", "2026-06-01", "-5.00", "VENDOR A")]
    _import_some(env, simplefin_server, txns)
    fake_ai.respond({"map": {
        "VENDOR A": {"category": "expenses:not-a-real-category",
                     "confidence": "high"},
    }})
    run_categorize(env.cfg)
    pend = journal.parse_pending(env.repo / "pending.journal")
    assert pend[0].category == journal.UNCATEGORIZED


def test_every_decision_logged_before_use(env, simplefin_server, fake_ai):
    txns = [txn("x4", "2026-06-01", "-20.00", "CINEMA")]
    _import_some(env, simplefin_server, txns)
    declare_account(env.repo, "expenses:entertainment")
    fake_ai.respond({"map": {
        "CINEMA": {"category": "expenses:entertainment",
                   "confidence": "high", "rationale": "movie theater"},
    }})
    run_categorize(env.cfg)
    suggests = [e for e in ailog.read_all(env.repo)
                if e["event"] == "suggest"]
    assert suggests and suggests[0]["txn_id"] == "x4"
    for field in ("ts", "payee", "amount", "suggestion", "confidence",
                  "model"):
        assert field in suggests[0]


def test_data_minimization_contract():
    """Only payee/description/amount/date/source account ever leave (PRD 6)."""
    leaky = {
        "id": "t1", "date": "2026-06-01", "payee": "X", "description": "y",
        "amount": "$-1.00", "source_account": "assets:checking",
        "account_number": "12345678", "balance": "9999.99",
        "full_history": ["..."],
    }
    sent = ai.minimized_payload([leaky])[0]
    assert set(sent) <= set(ai.ALLOWED_TXN_FIELDS)
    assert "account_number" not in json.dumps(sent)


def test_categorizer_write_surface(env, simplefin_server, fake_ai):
    """The categorizer never touches main.journal, rules, or git history."""
    from budge.gitutil import head_commit

    txns = [txn("x5", "2026-06-01", "-7.00", "VENDOR B")]
    _import_some(env, simplefin_server, txns)
    declare_account(env.repo, "expenses:misc")
    main_before = (env.repo / "main.journal").read_text()
    rules_before = (env.repo / "import/rules/checking.rules").read_text()
    head_before = head_commit(env.repo)
    fake_ai.respond({"default": {"category": "expenses:misc",
                                 "confidence": "low"}})
    run_categorize(env.cfg)
    assert (env.repo / "main.journal").read_text() == main_before
    assert (env.repo / "import/rules/checking.rules").read_text() \
        == rules_before
    assert head_commit(env.repo) == head_before  # no commit by categorizer
