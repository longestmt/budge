"""SimpleFIN claim exchange (7.1), scaffold idempotency (A11), notifier
boundary (7.7), CLI surface (prime-directive corollary)."""

import pytest

from budge import simplefin
from budge.scaffold import scaffold


def test_claim_exchange(simplefin_server):
    token = simplefin_server.setup_token("fresh")
    access_url = simplefin.claim(token)
    assert access_url == simplefin_server.access_url


def test_already_claimed_token_clear_error(simplefin_server):
    token = simplefin_server.setup_token("used")
    simplefin.claim(token)
    with pytest.raises(simplefin.SimpleFINError, match="ALREADY BEEN CLAIMED"):
        simplefin.claim(token)


def test_garbage_token_clear_error():
    with pytest.raises(simplefin.SimpleFINError):
        simplefin.claim("definitely not base64 of a url!!")


def test_scaffold_idempotent_A11(env):
    """Re-running the scaffold changes nothing destructive."""
    main = env.repo / "main.journal"
    main.write_text(main.read_text() + "\n; operator customization\n")
    scaffold(env.repo)  # second run
    assert "; operator customization" in main.read_text()


def test_gitignore_excludes_secret_shapes(env):
    gi = (env.repo / ".gitignore").read_text()
    for pattern in ("*.env", "secrets*", "*.key", "*.pem"):
        assert pattern in gi


def test_no_hledger_wrapper_subcommands():
    """Corollary in PRD section 1: no `budge balance`, no `budge report`."""
    from budge.cli import main
    for forbidden in ("balance", "report", "register", "stats"):
        with pytest.raises(SystemExit) as exc:
            main([forbidden])
        assert exc.value.code == 2  # argparse: unknown command


def test_notifier_is_outbound_only():
    """OpenClaw hard boundary: the notify module contains no repo writes."""
    import inspect
    from budge import notify
    source = inspect.getsource(notify)
    for needle in ("write_text", "open(", "commit", "push(", "add_vendor",
                   "write_pending"):
        assert needle not in source.replace("urllib.request.urlopen",
                                            "URLOPEN"), needle


def test_choose_enumerated(answers, capsys):
    from budge.util import choose
    options = [("a", "Option A"), ("b", "Option B"), ("c", "Option C")]
    answers.extend(["2"])
    assert choose("pick one", options) == "b"
    answers.extend([""])             # blank -> default (first option)
    assert choose("pick one", options) == "a"
    answers.extend(["9", "3"])       # out of range -> re-prompt
    assert choose("pick one", options) == "c"
    out = capsys.readouterr().out
    assert "1) Option A" in out and "3) Option C" in out


def test_review_nudge_counts_pending(env, capsys):
    from budge.notify import notify_review_ready
    from budge import journal
    entries = [journal.Pending(
        date="2026-06-08", payee="X", sf_id=f"n{i}",
        source_account="assets:checking", amount="$-1.00")
        for i in range(4)]
    journal.write_pending(env.repo / "pending.journal", entries)
    notify_review_ready(env.cfg)  # no URL configured -> prints payload
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "review ready — 4 transactions pending" in out
