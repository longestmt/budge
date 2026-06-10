"""Interactive, idempotent setup (PRD section 7.1) — `budge setup`.

Safe to re-run: every step detects existing state and skips or confirms.
Prerequisite *installation* lives in setup.sh (needs root); this command
verifies, collects inputs, claims SimpleFIN, scaffolds, backfills, installs
units, and hands off to the budget wizard.
"""

from __future__ import annotations

import configparser
import datetime as dt
import os
import shutil
import stat
from pathlib import Path

from . import simplefin
from .config import Config, conf_path, config_dir, secrets_path
from .fetch import run_fetch
from .gitutil import commit_all, git
from .scaffold import (declare_account, load_accounts, save_accounts,
                       scaffold, seed_account_rules)
from .util import confirm, die, dry, prompt, run, say, warn

UNITS = ["budge-fetch", "budge-categorize", "budge-push",
         "budge-review-nudge", "budge-notify@"]


def _check_prereqs(cfg) -> None:
    say("— prerequisites —")
    missing = []
    for tool, probe in [("hledger", [cfg.hledger_bin(), "--version"]),
                        ("git", ["git", "--version"])]:
        try:
            out = run(probe).stdout.strip().splitlines()[0]
            say(f"  {out}")
        except Exception:
            missing.append(tool)
    if missing:
        die(f"missing: {', '.join(missing)} — run setup.sh (as root) first, "
            "or install them via apt")
    if not shutil.which("paisa") and not shutil.which("podman") \
            and not shutil.which("docker"):
        warn("neither paisa nor a container runtime found — the dashboard "
             "step will print instructions instead of installing")


def _collect_config(cfg) -> Config:
    say("\n— configuration —")
    config_dir().mkdir(parents=True, exist_ok=True)
    ini = configparser.ConfigParser()
    if conf_path().exists():
        ini.read(conf_path(), encoding="utf-8")
    for section in ("repo", "ai", "schedule", "notify"):
        if not ini.has_section(section):
            ini.add_section(section)

    repo = prompt("data repo path",
                  ini.get("repo", "path", fallback="~/budge"))
    ini.set("repo", "path", repo)

    provider = prompt("AI provider (openai-compatible | anthropic)",
                      ini.get("ai", "provider",
                              fallback="openai-compatible"))
    ini.set("ai", "provider", provider)
    default_url = ("https://ollama.com/v1" if provider == "openai-compatible"
                   else "https://api.anthropic.com")
    ini.set("ai", "base_url",
            prompt("AI base URL", ini.get("ai", "base_url",
                                          fallback=default_url)))
    ini.set("ai", "model", prompt("AI model name",
                                  ini.get("ai", "model", fallback="")))

    ini.set("schedule", "fetch",
            prompt("fetch schedule (systemd OnCalendar)",
                   ini.get("schedule", "fetch", fallback="*-*-* 06:00:00")))
    ini.set("schedule", "categorize",
            prompt("categorize schedule",
                   ini.get("schedule", "categorize",
                           fallback="*-*-* 06:20:00")))
    ini.set("schedule", "push",
            prompt("push schedule",
                   ini.get("schedule", "push", fallback="*-*-* 06:40:00")))

    ini.set("notify", "openclaw_url",
            prompt("OpenClaw notification endpoint URL (blank to set later)",
                   ini.get("notify", "openclaw_url", fallback="")))
    ini.set("notify", "review_day",
            prompt("review day (for the weekly nudge)",
                   ini.get("notify", "review_day", fallback="Sat")))

    if not dry(f"write {conf_path()}"):
        with open(conf_path(), "w", encoding="utf-8") as f:
            ini.write(f)
        say(f"wrote {conf_path()}")
    return Config()


def _collect_secrets(cfg) -> Config:
    say("\n— secrets (stored OUTSIDE the repo, chmod 600) —")
    secrets = dict(cfg.secrets)

    if secrets.get("SIMPLEFIN_ACCESS_URL"):
        say("SimpleFIN access URL already stored; keeping it "
            "(delete it from secrets.env to re-claim)")
    else:
        token = prompt("SimpleFIN SETUP TOKEN (one-time; never persisted)")
        if token:
            try:
                access_url = simplefin.claim(token)
            except simplefin.SimpleFINError as e:
                die(str(e))
            secrets["SIMPLEFIN_ACCESS_URL"] = access_url
            say("claim exchange OK — permanent access URL stored")
        else:
            warn("no token given; fetch will not work until you re-run setup")

    if not secrets.get("AI_API_KEY"):
        key = prompt("AI API key (blank if none needed)")
        if key:
            secrets["AI_API_KEY"] = key

    lines = ["# budge secrets — never commit this file anywhere"]
    lines += [f"{k}={v}" for k, v in secrets.items()]
    if not dry(f"write {secrets_path()} (mode 600)"):
        secrets_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(secrets_path(), stat.S_IRUSR | stat.S_IWUSR)  # 600
        say(f"wrote {secrets_path()} (mode 600)")
    return Config()


def _map_accounts(cfg) -> None:
    repo = cfg.repo
    existing = load_accounts(repo)
    if existing:
        say(f"\n{len(existing)} accounts already mapped; keeping them")
        return
    if not cfg.simplefin_access_url:
        warn("no SimpleFIN access URL; skipping account mapping")
        return
    say("\n— account mapping —")
    import time
    data = simplefin.get_accounts(cfg.simplefin_access_url,
                                  start_date=int(time.time()))
    accounts = []
    for sf in data.get("accounts", []):
        name = sf.get("name", sf["id"])
        org = (sf.get("org") or {}).get("name", "")
        say(f"\nfound: {org} / {name} (balance {sf.get('balance', '?')})")
        default_slug = "-".join(
            "".join(ch if ch.isalnum() else " " for ch in name)
            .lower().split())[:30] or "account"
        slug = prompt("  short slug for files", default_slug)
        kind = "liabilities:" if confirm(
            "  is this a credit card / liability?", default=False) \
            else "assets:"
        hl_account = prompt("  hledger account name", kind + slug)
        accounts.append({
            "id": sf["id"], "name": f"{org} {name}".strip(),
            "slug": slug, "account": hl_account, "currency": "$",
        })
        declare_account(repo, hl_account)
        if not dry(f"seed rules file for {slug}"):
            seed_account_rules(repo, accounts[-1])
    if not dry("write import/accounts.json"):
        save_accounts(repo, accounts)
    commit_all(repo, "budge setup: account mapping + seeded transfer rules")
    say(f"mapped {len(accounts)} accounts; "
        "transfer/payment rule patterns seeded per account (PRD 7.3)")


def _github_remote(cfg) -> None:
    repo = cfg.repo
    remotes = git(repo, "remote", check=False).stdout.split()
    if remotes:
        say("\ngit remote already configured")
        return
    url = prompt("\nGitHub remote URL for the journal repo (blank to skip)")
    if url and not dry(f"git remote add origin {url}"):
        git(repo, "remote", "add", "origin", url)
        say("remote added — pushes use your ambient git auth "
            "(SSH key or credential helper)")


def _backfill(cfg) -> None:
    repo = cfg.repo
    main = (repo / "main.journal").read_text(encoding="utf-8") \
        if (repo / "main.journal").exists() else ""
    if "simplefin_id:" in main:
        say("\ntransactions already imported — skipping the 90-day backfill "
            "(re-runs are non-destructive)")
        return
    if not cfg.simplefin_access_url or not load_accounts(repo):
        warn("backfill skipped (need SimpleFIN access + mapped accounts)")
        return
    say("\n— 90-day backfill (opening balances + balance assertions) —")
    run_fetch(cfg, backfill_days=90, interactive=True)


def _render_units(cfg) -> Path:
    """Render unit templates with concrete paths into <repo>/systemd/."""
    import budge
    pkg_dir = Path(budge.__file__).resolve().parent.parent
    src = pkg_dir / "systemd"
    dst = cfg.repo / "systemd"
    if dry(f"render systemd unit templates into {dst}"):
        return dst
    dst.mkdir(exist_ok=True)
    budge_bin = shutil.which("budge") or "/usr/local/bin/budge"
    subs = {
        "@BUDGE@": budge_bin,
        "@USER@": os.environ.get("USER", "root"),
        "@REPO@": str(cfg.repo),
        "@CONFIG_DIR@": str(config_dir()),
        "@SCHEDULE_FETCH@": cfg.schedule("fetch"),
        "@SCHEDULE_CATEGORIZE@": cfg.schedule("categorize"),
        "@SCHEDULE_PUSH@": cfg.schedule("push"),
        "@REVIEW_DAY@": cfg.review_day,
    }
    if not src.exists():
        warn(f"unit templates not found at {src}; skipping")
        return dst
    for tpl in sorted(src.glob("*.in")):
        text = tpl.read_text(encoding="utf-8")
        for key, value in subs.items():
            text = text.replace(key, value)
        (dst / tpl.name[:-3]).write_text(text, encoding="utf-8")
    for static in sorted(src.glob("*.service")) + sorted(src.glob("*.timer")):
        shutil.copy(static, dst / static.name)
    say(f"rendered systemd units into {dst}")
    return dst


def _install_units(cfg, rendered: Path) -> None:
    target = Path("/etc/systemd/system")
    if dry("install + enable systemd timers"):
        return
    if os.access(target, os.W_OK):
        for unit in rendered.glob("budge-*"):
            shutil.copy(unit, target / unit.name)
        run(["systemctl", "daemon-reload"], check=False)
        for timer in ["budge-fetch.timer", "budge-categorize.timer",
                      "budge-push.timer", "budge-review-nudge.timer"]:
            run(["systemctl", "enable", "--now", timer], check=False)
        say("systemd timers installed and enabled")
    else:
        say(
            "\nTo install the timers (needs root):\n"
            f"  sudo cp {rendered}/budge-* /etc/systemd/system/\n"
            "  sudo systemctl daemon-reload\n"
            "  sudo systemctl enable --now budge-fetch.timer "
            "budge-categorize.timer budge-push.timer "
            "budge-review-nudge.timer"
        )


def _paisa(cfg) -> None:
    repo = cfg.repo
    paisa_yaml = repo / "paisa.yaml"
    if not paisa_yaml.exists() and not dry(f"write {paisa_yaml}"):
        paisa_yaml.write_text(
            "# paisa.yaml — stock Paisa dashboard config (no modifications\n"
            "# to Paisa itself; it simply reads the hledger journal).\n"
            f"journal_path: {repo / 'main.journal'}\n"
            f"db_path: {config_dir() / 'paisa.db'}\n"
            "ledger_cli: hledger\n"
            "default_currency: USD\n"
            "locale: en-US\n",
            encoding="utf-8",
        )
        say(f"wrote {paisa_yaml}")
    say("Paisa serves the dashboard via the rendered paisa.service "
        "(container) — see systemd/ in the repo.")


def run_setup(cfg) -> None:
    say("budge setup — interactive bootstrap (safe to re-run)\n")
    _check_prereqs(cfg)
    cfg = _collect_config(cfg)
    cfg = _collect_secrets(cfg)
    scaffold(cfg.repo)
    _map_accounts(cfg)
    _github_remote(cfg)
    _backfill(cfg)
    rendered = _render_units(cfg)
    _install_units(cfg, rendered)
    _paisa(cfg)
    commit_all(cfg.repo, "budge setup: configuration artifacts")

    # Budget wizard, bootstrap mode — setup succeeds even if skipped (7.1)
    if confirm("\nRun the budget planning wizard now (analyzes the real "
               "backfilled data)?", default=True):
        try:
            from .plan import run_plan
            run_plan(cfg, from_setup=True)
        except Exception as e:
            warn(f"wizard did not finish ({e}) — run `budge plan` later; "
                 "setup itself is complete")
    else:
        say("skipped — run `budge plan` whenever you're ready")

    say(
        "\n=== what happens next ===\n"
        f"  every morning  budge-fetch pulls new transactions into "
        f"{cfg.repo / 'main.journal'}\n"
        "                 (rule-matched: cleared; the rest: pending via AI)\n"
        "  after fetch    budge-categorize suggests categories for the rest\n"
        "  after both     budge-push syncs the repo to your remote\n"
        "  weekly         OpenClaw nudges you; run `budge review` "
        "(~10 minutes)\n"
        "  anytime        the Paisa dashboard answers “how much is left?”\n"
        "  reporting      plain hledger, e.g. "
        "hledger -f main.journal balance --budget\n"
    )
