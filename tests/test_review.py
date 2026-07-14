"""Review & promote tests: acceptance A7, A8; outcome logging; overrides."""

import datetime as dt

import pytest

from budge import ailog, hledger, journal
from budge.categorize import run_categorize
from budge.fetch import run_fetch
from budge.gitutil import head_commit
from budge.review import (_category_options, _prompt_category, correct_single,
                          correct_vendor, promote)
from budge.scaffold import MAIN_TRANSACTION_MARKER, declare_account

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


def _repo_files(repo):
    """Snapshot non-git files so failed promotion tests catch every write."""
    return {
        path.relative_to(repo): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


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
    new = txn("r9", dt.date.today().isoformat(), "-5.75", "BLUE BOTTLE")
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
    assert (env.repo / "pending.journal").read_text(encoding="utf-8") \
        == journal.PENDING_HEADER
    main = (env.repo / "main.journal").read_text()
    assert "* BLUE BOTTLE" in main
    ok, out = hledger.check(env.repo / "main.journal")
    assert ok, out

    outcomes = {e["txn_id"]: e for e in ailog.read_all(env.repo)
                if e["event"] == "outcome"}
    assert outcomes["r1"]["result"] == "accepted"
    assert outcomes["r4"]["result"] == "overridden"  # AI said groceries
    assert outcomes["r4"]["final"] == "expenses:dining"


def test_promote_preserves_pending_order_before_balance_assertion(env):
    """Clearing a pending txn must not move it behind a later assertion."""
    declare_account(env.repo, "income:contributions")
    pending = journal.Pending(
        date="2026-07-14",
        payee="PLAN CONTRIBUTION",
        sf_id="order-1",
        source_account="assets:checking",
        amount="$120.08",
        category="income:contributions",
        origin="ai",
        suggested="income:contributions",
    )
    journal.write_pending(env.repo / "pending.journal", [pending])
    assertion = (
        "2026-07-14 * balance assertion (simplefin)\n"
        "    assets:checking                           $0 = $120.08\n"
    )
    main_path = env.repo / "main.journal"
    main_path.write_text(main_path.read_text(encoding="utf-8")
                         + "\n" + assertion, encoding="utf-8")

    ok, output = hledger.check(main_path)
    assert ok, output
    assert promote(env.cfg)

    assert (env.repo / "pending.journal").read_text(encoding="utf-8") \
        == journal.PENDING_HEADER
    main = main_path.read_text(encoding="utf-8")
    assert (main.index(MAIN_TRANSACTION_MARKER)
            < main.index("2026-07-14 * PLAN CONTRIBUTION")
            < main.index("2026-07-14 * balance assertion"))
    ok, output = hledger.check(main_path)
    assert ok, output


@pytest.mark.parametrize("marker_count", [0, 2])
def test_promote_requires_exactly_one_transaction_marker(
        env, simplefin_server, fake_ai, marker_count, capsys):
    _seed(env, simplefin_server, fake_ai)
    main_path = env.repo / "main.journal"
    main = main_path.read_text(encoding="utf-8")
    if marker_count == 0:
        main = main.replace(MAIN_TRANSACTION_MARKER,
                            "; operator transaction section")
    else:
        main += "\n" + MAIN_TRANSACTION_MARKER + "\n"
    main_path.write_text(main, encoding="utf-8")
    files_before = _repo_files(env.repo)
    head_before = head_commit(env.repo)

    with pytest.raises(SystemExit):
        promote(env.cfg)

    error = capsys.readouterr().err
    assert ("missing" if marker_count == 0 else "ambiguous") in error
    assert "expected exactly one line" in error
    assert _repo_files(env.repo) == files_before
    assert head_commit(env.repo) == head_before


def test_promote_rehearsal_failure_modifies_no_real_files(
        env, simplefin_server, fake_ai, monkeypatch):
    _seed(env, simplefin_server, fake_ai)
    files_before = _repo_files(env.repo)
    head_before = head_commit(env.repo)
    real_check = hledger.check
    calls = 0

    def fail_second_check(path, extra_checks=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            return False, "rehearsed books do not balance"
        return real_check(path, extra_checks)

    monkeypatch.setattr(hledger, "check", fail_second_check)
    with pytest.raises(SystemExit):
        promote(env.cfg)

    assert calls == 2
    assert _repo_files(env.repo) == files_before
    assert head_commit(env.repo) == head_before


def test_multiple_promotions_preserve_batch_order(env):
    declare_account(env.repo, "expenses:test")

    def pending(tid, payee):
        return journal.Pending(
            date="2026-07-14",
            payee=payee,
            sf_id=tid,
            source_account="assets:transfers",
            amount="$-1.00",
            category="expenses:test",
            origin="manual",
        )

    journal.write_pending(env.repo / "pending.journal", [
        pending("batch-1a", "FIRST A"),
        pending("batch-1b", "FIRST B"),
    ])
    assert promote(env.cfg)
    journal.write_pending(env.repo / "pending.journal", [
        pending("batch-2", "SECOND"),
    ])
    assert promote(env.cfg)

    main = (env.repo / "main.journal").read_text(encoding="utf-8")
    assert (main.index(MAIN_TRANSACTION_MARKER)
            < main.index("; simplefin_id: batch-2")
            < main.index("; simplefin_id: batch-1a")
            < main.index("; simplefin_id: batch-1b"))
    for tid in ("batch-1a", "batch-1b", "batch-2"):
        assert main.count(f"; simplefin_id: {tid}") == 1
    assert not journal.parse_pending(env.repo / "pending.journal")
    ok, output = hledger.check(env.repo / "main.journal")
    assert ok, output


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
