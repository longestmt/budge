"""The interactive `budge review` loop end-to-end (scripted answers)."""

from budge import journal
from budge.categorize import run_categorize
from budge.fetch import run_fetch
from budge.review import run_review
from budge.scaffold import declare_account

from conftest import checking_account, consistent_balance, txn


def test_review_session_approve_correct_promote(env, simplefin_server,
                                                fake_ai, answers, capsys):
    txns = [
        txn("i1", "2026-05-03", "-4.50", "BLUE BOTTLE"),
        txn("i2", "2026-05-10", "-5.25", "BLUE BOTTLE"),
        txn("i3", "2026-05-20", "-60.00", "SAFEWAY"),
    ]
    simplefin_server.accounts = [
        checking_account(txns, consistent_balance(400.0, txns))]
    run_fetch(env.cfg, backfill_days=90, interactive=False)
    for cat in ("expenses:dining", "expenses:groceries", "expenses:coffee"):
        declare_account(env.repo, cat)
    fake_ai.respond({"map": {
        "BLUE BOTTLE": {"category": "expenses:dining",
                        "confidence": "medium"},
        "SAFEWAY": {"category": "expenses:groceries", "confidence": "high"},
    }})
    run_categorize(env.cfg)

    answers.extend([
        "v",                  # biggest group first (BLUE BOTTLE, count desc)
        "expenses:coffee",    # vendor correction -> rule + regeneration
        "a",                  # approve SAFEWAY
        "y",                  # promote
    ])
    run_review(env.cfg)

    out = capsys.readouterr().out
    assert "BLUE BOTTLE" in out.split("SAFEWAY")[0]  # sorted by count desc
    assert "lowest confidence" in out

    # everything ended up cleared in main; pending empty; rule persisted
    assert not journal.parse_pending(env.repo / "pending.journal")
    main = (env.repo / "main.journal").read_text()
    assert "expenses:coffee" in main and "* SAFEWAY" in main
    rules = (env.repo / "import/rules/checking.rules").read_text()
    assert "BLUE\\ BOTTLE" in rules


def test_review_single_transaction_prompts_for_category(
        env, simplefin_server, fake_ai, answers, capsys):
    txns = [txn("i1", "2026-05-03", "-4.50", "BLUE BOTTLE")]
    simplefin_server.accounts = [
        checking_account(txns, consistent_balance(400.0, txns))]
    run_fetch(env.cfg, backfill_days=90, interactive=False)
    for cat in ("expenses:dining", "expenses:coffee"):
        declare_account(env.repo, cat)
    fake_ai.respond({"map": {
        "BLUE BOTTLE": {"category": "expenses:dining",
                        "confidence": "medium"},
    }})
    run_categorize(env.cfg)

    answers.extend([
        "s",                   # recategorize a single transaction
        "3",                   # choose expenses:coffee from category list
        "a",                   # approve the now-corrected group
        "n",                   # don't promote in this test
    ])
    run_review(env.cfg)

    out = capsys.readouterr().out
    assert "recategorizing one transaction" in out
    assert "choose the new category for this transaction" in out
    assert "choose a category" in out
    pending = journal.parse_pending(env.repo / "pending.journal")
    assert pending[0].category == "expenses:coffee"
    assert pending[0].origin == "manual"
