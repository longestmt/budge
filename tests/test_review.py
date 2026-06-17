"""Review & promote tests: acceptance A7, A8; outcome logging; overrides."""

import pytest

from budge import ailog, hledger, journal
from budge.categorize import run_categorize
from budge.fetch import run_fetch
from budge.gitutil import head_commit
from budge.review import (_category_options, _prompt_category, correct_single,
                          correct_vendor, promote)
from budge.scaffold import declare_account

from conftest import checking_account, consistent_balance, txn


def _seed(env, simplefin_server, fake_ai):
    txns = [
        txn("r1", "2026-05-03", "-4.50", "BLUE BOTTLE"),
        txn("r2", "2026-05-10", "-5.25", "BLUE BOTTLE"),
        txn("r3", "2026-05-17", "-6.00", "BLUE BOTTLE"),
        txn("r4", "2026-05-20", "-60.00", "SAFEWAY"),
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


def test_vendor_correction_A7(env, simplefin_server, fake_ai):
    _seed(env, simplefin_server, fake_ai)
    pend = journal.parse_pending(env.repo / "pending.journal")
    group = [e for e in pend if e.payee == "BLUE BOTTLE"]
    assert len(group) == 3

    correct_vendor(env.cfg, "BLUE BOTTLE", group, "expenses:coffee")

    # the rule was written...
    rules = (env.repo / "import/rules/checking.rules").read_text()
    assert "expenses:coffee" in rules
    # ...every matching pending txn updated at once (regeneration moved them
    # to main.journal, cleared, with the corrected category)
    pend = journal.parse_pending(env.repo / "pending.journal")
    assert not [e for e in pend if e.payee == "BLUE BOTTLE"]
    main = (env.repo / "main.journal").read_text()
    assert main.count("expenses:coffee") >= 3
    ok, out = hledger.check(env.repo / "main.journal")
    assert ok, out

    # ...and the vendor never re-enters pending on the next fetch
    new = txn("r9", "2026-06-08", "-5.75", "BLUE BOTTLE")
    txns = simplefin_server.accounts[0]["transactions"] + [new]
    simplefin_server.accounts = [
        checking_account(txns, consistent_balance(400.0, txns))]
    run_fetch(env.cfg)
    pend = journal.parse_pending(env.repo / "pending.journal")
    assert not [e for e in pend if e.payee == "BLUE BOTTLE"]
    assert "r9" in journal.journal_sf_ids(env.repo / "main.journal")


def test_promote_gate_A8(env, simplefin_server, fake_ai):
    _seed(env, simplefin_server, fake_ai)
    # deliberately malform pending.journal
    with open(env.repo / "pending.journal", "a") as f:
        f.write("\n2026-06-09 ! BROKEN ENTRY\n"
                "    expenses:dining   $5.00\n"
                "    expenses:dining   $5.00\n"
                "    assets:checking   $-5.00\n")  # does not balance
    main_before = (env.repo / "main.journal").read_text()
    head_before = head_commit(env.repo)

    with pytest.raises(SystemExit):
        promote(env.cfg)

    # nothing written, committed, or pushed
    assert (env.repo / "main.journal").read_text() == main_before
    assert head_commit(env.repo) == head_before
    assert journal.parse_pending(env.repo / "pending.journal")


def test_promote_flips_and_logs_outcomes(env, simplefin_server, fake_ai):
    _seed(env, simplefin_server, fake_ai)
    pend = journal.parse_pending(env.repo / "pending.journal")
    target = [e for e in pend if e.payee == "SAFEWAY"][0]
    correct_single(env.cfg, target, "expenses:dining")  # operator override

    assert promote(env.cfg)

    # pending truncated; entries now cleared (*) in main.journal
    assert not journal.parse_pending(env.repo / "pending.journal")
    main = (env.repo / "main.journal").read_text()
    assert "* BLUE BOTTLE" in main
    ok, out = hledger.check(env.repo / "main.journal")
    assert ok, out

    outcomes = {e["txn_id"]: e for e in ailog.read_all(env.repo)
                if e["event"] == "outcome"}
    assert outcomes["r1"]["result"] == "accepted"
    assert outcomes["r4"]["result"] == "overridden"  # AI said groceries
    assert outcomes["r4"]["final"] == "expenses:dining"


def test_manual_override_survives_regeneration(env, simplefin_server,
                                               fake_ai):
    from budge.categorize import regenerate

    _seed(env, simplefin_server, fake_ai)
    pend = journal.parse_pending(env.repo / "pending.journal")
    target = [e for e in pend if e.payee == "SAFEWAY"][0]
    correct_single(env.cfg, target, "expenses:dining")

    regenerate(env.cfg)  # pending is derived; the one-off must persist

    pend = journal.parse_pending(env.repo / "pending.journal")
    safeway = [e for e in pend if e.payee == "SAFEWAY"][0]
    assert safeway.category == "expenses:dining"
    assert safeway.origin == "manual"


def test_promote_empty_pending_noop(env):
    assert promote(env.cfg) is False


def test_prompt_category_can_select_existing_by_number(env, answers):
    declare_account(env.repo, "expenses:coffee")
    idx = _category_options(env.repo).index("expenses:coffee") + 1

    answers.append(str(idx))

    assert _prompt_category(env.repo) == "expenses:coffee"


def test_prompt_category_can_add_new_category(env, answers):
    answers.extend(["n", "expenses:parking", "y"])

    assert _prompt_category(env.repo) == "expenses:parking"
    accounts = (env.repo / "accounts.journal").read_text()
    assert "account expenses:parking" in accounts


def test_dry_run_correction_and_promote_A12(env, simplefin_server, fake_ai):
    from budge import util
    from budge.gitutil import git, head_commit

    _seed(env, simplefin_server, fake_ai)
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-m", "snapshot", "-q")
    head = head_commit(env.repo)
    rules_before = (env.repo / "import/rules/checking.rules").read_text()
    pending_before = (env.repo / "pending.journal").read_text()
    main_before = (env.repo / "main.journal").read_text()

    util.DRY_RUN = True
    try:
        pend = journal.parse_pending(env.repo / "pending.journal")
        group = [e for e in pend if e.payee == "BLUE BOTTLE"]
        correct_vendor(env.cfg, "BLUE BOTTLE", group, "expenses:coffee")
        promote(env.cfg)
    finally:
        util.DRY_RUN = False

    assert (env.repo / "import/rules/checking.rules").read_text() \
        == rules_before
    assert (env.repo / "pending.journal").read_text() == pending_before
    assert (env.repo / "main.journal").read_text() == main_before
    assert head_commit(env.repo) == head
    assert git(env.repo, "status", "--porcelain").stdout.strip() == ""
