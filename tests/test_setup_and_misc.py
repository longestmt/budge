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
    with pytest.raises(simplefin.SimpleFINError, match="already claimed"):
        simplefin.claim(token)


def test_garbage_token_clear_error():
    with pytest.raises(simplefin.SimpleFINError):
        simplefin.claim("definitely not base64 of a url!!")


def test_token_decode_tolerates_real_world_paste():
    """Unpadded, URL-safe, line-wrapped, and direct-URL tokens all decode."""
    import base64
    url = "https://bridge.example/simplefin/claim/abc-123_XY"
    std = base64.b64encode(url.encode()).decode()
    assert simplefin.decode_setup_token(std) == url
    assert simplefin.decode_setup_token(std.rstrip("=")) == url   # no padding
    wrapped = std[:20] + "\n" + std[20:]                          # line wrap
    assert simplefin.decode_setup_token(wrapped) == url
    urlsafe = base64.urlsafe_b64encode(url.encode()).decode()
    assert simplefin.decode_setup_token(urlsafe) == url
    assert simplefin.decode_setup_token(url) == url               # direct URL


def test_corrupted_token_not_misreported_as_claimed(simplefin_server):
    """A truncated token must NOT produce the 'already claimed' message."""
    token = simplefin_server.setup_token("fresh2")
    truncated = token[: len(token) // 2]
    try:
        simplefin.claim(truncated)
        raised = None
    except simplefin.SimpleFINError as e:
        raised = str(e)
    assert raised is not None
    assert "already" not in raised.lower()


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


def test_choose_multi(answers):
    from budge.util import choose_multi
    options = [("a", "A"), ("b", "B"), ("c", "C")]
    answers.extend(["1,3"])
    assert choose_multi("pick", options) == {"a", "c"}
    answers.extend([""])             # blank keeps current selection
    assert choose_multi("pick", options, {"b"}) == {"b"}
    answers.extend(["0"])            # none
    assert choose_multi("pick", options, {"a", "b"}) == set()


def test_ui_config_parsing(env):
    from budge.config import Config
    (env.confdir / "budge.conf").write_text(
        (env.confdir / "budge.conf").read_text()
        + "[ui]\nenabled = paisa, hledger-ui\n")
    cfg = Config()
    assert cfg.ui_enabled == {"paisa", "hledger-ui"}
    assert Config().ui_enabled  # default (no section) includes paisa


def test_ui_command_updates_choice(env, answers, capsys):
    from budge.config import Config
    from budge.setup_cmd import run_ui
    answers.extend(["3"])            # hledger-ui only
    run_ui(Config())
    assert Config().ui_enabled == {"hledger-ui"}
    assert not (env.repo / "paisa.yaml").exists()   # paisa not configured
    out = capsys.readouterr().out
    assert "UI status" in out        # the unmissable summary block
    assert "hledger-ui" in out
    run_ui(Config(), show_only=True)
    out = capsys.readouterr().out
    assert "[x]" in out and "hledger-ui" in out


def test_ui_status_flags_missing_tools(env, capsys):
    from budge.config import Config
    from budge.setup_cmd import _ui_status
    (env.confdir / "budge.conf").write_text(
        (env.confdir / "budge.conf").read_text()
        + "[ui]\nenabled = hledger-web, hledger-textual\n")
    _ui_status(Config())
    out = capsys.readouterr().out
    # neither tool exists in the sandbox: both lines demand action
    assert out.count("ACTION NEEDED") == 2


def test_paisa_skipped_when_not_chosen(env):
    from budge.config import Config
    from budge.setup_cmd import _paisa
    (env.confdir / "budge.conf").write_text(
        (env.confdir / "budge.conf").read_text()
        + "[ui]\nenabled = hledger-ui\n")
    _paisa(Config())
    assert not (env.repo / "paisa.yaml").exists()


def test_cheatsheet_prints_recipes_without_running_them(capsys):
    from budge.cheatsheet import run_cheatsheet
    run_cheatsheet()
    out = capsys.readouterr().out
    assert "hledger register desc:" in out          # vendor recipe
    assert "incomestatement" in out                  # statement recipe
    assert "--budget" in out                         # budget recipe
    run_cheatsheet("vendor")
    out = capsys.readouterr().out
    assert "desc:KROGER" in out and "incomestatement -M" not in out
    run_cheatsheet("zzz-no-match")
    assert "nothing matching" in capsys.readouterr().out


def test_github_remote_normalization():
    from budge.setup_cmd import normalize_github_remote as norm
    ssh = "git@github.com:longestmt/longestbudget.git"
    https = "https://github.com/longestmt/longestbudget.git"
    for given in ("longestmt/longestbudget", https, ssh,
                  "https://github.com/longestmt/longestbudget",
                  "github.com/longestmt/longestbudget"):
        assert norm(given, "ssh") == ssh, given
        assert norm(given, "https") == https, given
    # non-GitHub URLs pass through untouched... if not owner/name shaped
    assert norm("ssh://git.mybox.lan:2222/budge",
                "ssh") != "git@github.com:budge.git"


def test_first_push_sets_upstream(env, tmp_path):
    """A repo with a remote but no upstream must push, not fail (the
    budge-push timer hit this on night one)."""
    from budge.gitutil import git, push
    bare = tmp_path / "remote.git"
    git(env.repo, "init", "--bare", str(bare))
    git(env.repo, "remote", "add", "origin", str(bare))
    push(env.repo)   # first push: sets upstream
    push(env.repo)   # second push: normal path
    branches = git(env.repo, "ls-remote", "--heads", "origin").stdout
    assert "main" in branches


def test_ledger_file_configured_idempotently(env):
    from budge.setup_cmd import _configure_ledger_file
    from pathlib import Path
    _configure_ledger_file(env.cfg)
    _configure_ledger_file(env.cfg)  # re-run must not duplicate
    rc = (Path.home() / ".bashrc").read_text()
    expected = f'export LEDGER_FILE="{env.repo / "main.journal"}"'
    assert rc.count(expected) == 1
    # a stale LEDGER_FILE from an old repo path gets replaced
    (Path.home() / ".bashrc").write_text(
        'export LEDGER_FILE="/old/path/main.journal"\n')
    _configure_ledger_file(env.cfg)
    rc = (Path.home() / ".bashrc").read_text()
    assert "/old/path" not in rc and rc.count(expected) == 1


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
