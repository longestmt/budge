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
from .util import (banner, choose, choose_multi, confirm, die, dry, header,
                   paint, prompt, run, say, success, warn)

UI_OPTIONS = [
    ("paisa", "Paisa — household web dashboard on :7500 (recommended)"),
    ("hledger-web", "hledger-web — official hledger web UI on :5000 "
                    "(served view-only)"),
    ("hledger-ui", "hledger-ui — official terminal UI (on demand, "
                   "no service)"),
    ("hledger-textual", "hledger-textual — third-party Textual TUI via "
                        "pipx (CAN EDIT transactions; never edit "
                        "pending.journal with it — that file is derived)"),
]

UNITS = ["budge-fetch", "budge-categorize", "budge-push",
         "budge-review-nudge", "budge-notify@"]


def _hledger_version(cfg) -> tuple:
    import re as _re
    try:
        out = run([cfg.hledger_bin(), "--version"]).stdout
        m = _re.search(r"hledger (\d+)\.(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return (0, 0)


def _check_prereqs(cfg) -> None:
    header("prerequisites")
    missing = []
    for tool, probe in [("hledger", [cfg.hledger_bin(), "--version"]),
                        ("git", ["git", "--version"])]:
        try:
            out = run(probe).stdout.strip().splitlines()[0]
            where = shutil.which(probe[0]) or probe[0]
            say(f"  {out}  ({where})")
        except Exception:
            missing.append(tool)
    version = _hledger_version(cfg)
    if (1, 25) <= version < (1, 40):
        say(paint(
            "  advisory: this hledger works with budge, but the wider "
            "ecosystem\n  (hledger-textual and friends) expects >= 1.40. "
            "Debian's package lags;\n  official binaries from "
            "https://hledger.org/install belong in /usr/local/bin.",
            "yellow"))
    if missing:
        die(f"missing: {', '.join(missing)} — run setup.sh (as root) first, "
            "or install them via apt")
    if not shutil.which("paisa") and not shutil.which("podman") \
            and not shutil.which("docker"):
        warn("neither paisa nor a container runtime found — the dashboard "
             "step will print instructions instead of installing")


def _prompt_keep(question: str, current: str, fallback: str = "") -> str:
    """Prompt that tells the user a value already exists and Enter keeps it."""
    if current:
        return prompt(f"{question} (already set — press Enter to keep)",
                      current)
    return prompt(question, fallback)


def _mark_current(options: list, current_value) -> list:
    """Append a '← current' marker to the option matching the stored value."""
    return [
        (v, label + ("   ← current (Enter keeps it)" if v == current_value
                     else ""))
        for v, label in options
    ]


def _collect_ai(cfg, ini) -> str:
    """Provider, key, and model selection. Returns the API key (in memory
    only — it is written to secrets.env, never to budge.conf)."""
    from . import ai as ai_mod

    header("AI provider — categorizes what your rules don't catch")
    presets = {
        "https://ollama.com/v1": "ollama-cloud",
        "https://api.openai.com/v1": "openai",
        "https://api.anthropic.com": "anthropic",
    }
    stored_base = ini.get("ai", "base_url", fallback="")
    current_pick = presets.get(
        stored_base, "local" if stored_base else None)
    options = _mark_current([
        ("ollama-cloud", "Ollama cloud"),
        ("openai", "OpenAI"),
        ("anthropic", "Anthropic (Claude)"),
        ("local", "Local Ollama or another self-hosted "
                  "OpenAI-compatible server"),
    ], current_pick)
    default = next((i for i, (v, _) in enumerate(options)
                    if v == current_pick), 0)
    pick = choose("Which AI provider do you want to use?", options,
                  default=default)
    if pick == "ollama-cloud":
        provider, base_url = "openai-compatible", "https://ollama.com/v1"
    elif pick == "openai":
        provider, base_url = "openai-compatible", "https://api.openai.com/v1"
    elif pick == "anthropic":
        provider, base_url = "anthropic", "https://api.anthropic.com"
    else:
        provider = "openai-compatible"
        base_url = _prompt_keep(
            "server URL (for local Ollama this is usually "
            "http://localhost:11434/v1)",
            stored_base if current_pick == "local" else "",
            "http://localhost:11434/v1")
    ini.set("ai", "provider", provider)
    ini.set("ai", "base_url", base_url)

    existing_key = cfg.ai_api_key
    if existing_key:
        key = prompt("API key/token (blank = keep the one already stored)") \
            or existing_key
    else:
        key = prompt("API key/token for this provider (blank if none "
                     "needed, e.g. local Ollama)")

    # Enumerate the provider's models rather than asking for freehand typing.
    current = ini.get("ai", "model", fallback="")
    models = []
    try:
        models = ai_mod.list_models(provider, base_url, key)
    except Exception as e:
        warn(f"could not fetch the model list ({e}) — falling back to "
             "typing a name")
    if models:
        options = _mark_current([(m, m) for m in models[:30]], current)
        options.append(("__other__", "other (type a model name)"))
        default = next((i for i, (v, _) in enumerate(options)
                        if v == current), 0)
        model = choose("Which model should categorize transactions?",
                       options, default=default)
        if model == "__other__":
            model = prompt("model name", current)
    else:
        model = _prompt_keep("AI model name", current)
    ini.set("ai", "model", model)
    return key


def _collect_schedule(ini) -> None:
    header("sync frequency")
    stored = ini.get("schedule", "fetch", fallback="")
    by_fetch = {"*-*-* 06:00:00": "daily", "*-*-* 00/4:00:00": "4h",
                "*-*-* *:00:00": "1h"}
    current = by_fetch.get(stored, "custom" if stored else None)
    options = _mark_current([
        ("daily", "Once a day, early morning (banks usually post "
                  "transactions overnight)"),
        ("4h", "Every 4 hours"),
        ("1h", "Every hour"),
        ("custom", "Custom (raw systemd OnCalendar expressions — "
                   "expert mode)"),
    ], current)
    default = next((i for i, (v, _) in enumerate(options) if v == current), 0)
    freq = choose(
        "How often should budge pull new transactions from your bank?",
        options, default=default)
    presets = {
        "daily": ("*-*-* 06:00:00", "*-*-* 06:20:00", "*-*-* 06:40:00",
                  "6:00 am"),
        "4h": ("*-*-* 00/4:00:00", "*-*-* 00/4:20:00", "*-*-* 00/4:40:00",
               "midnight, 4 am, 8 am, ... (every 4 hours)"),
        "1h": ("*-*-* *:00:00", "*-*-* *:20:00", "*-*-* *:40:00",
               "the top of every hour"),
    }
    if freq == "custom":
        fetch = prompt("when to fetch (OnCalendar)",
                       ini.get("schedule", "fetch",
                               fallback="*-*-* 06:00:00"))
        cat = prompt("when to categorize (should be after fetch)",
                     ini.get("schedule", "categorize",
                             fallback="*-*-* 06:20:00"))
        push = prompt("when to push (should be after both)",
                      ini.get("schedule", "push",
                              fallback="*-*-* 06:40:00"))
        human = "your custom schedule"
    else:
        fetch, cat, push, human = presets[freq]
    ini.set("schedule", "fetch", fetch)
    ini.set("schedule", "categorize", cat)
    ini.set("schedule", "push", push)
    say(f"bank sync at {human}; AI categorization runs 20 minutes later, "
        "git push 20 minutes after that")


def _collect_config(cfg):
    """Returns (Config, ai_api_key)."""
    header("configuration")
    config_dir().mkdir(parents=True, exist_ok=True)
    ini = configparser.ConfigParser()
    if conf_path().exists():
        ini.read(conf_path(), encoding="utf-8")
    for section in ("repo", "ai", "schedule", "notify"):
        if not ini.has_section(section):
            ini.add_section(section)

    repo = _prompt_keep("where should your books live? (a new private git "
                        "repo is created here)",
                        ini.get("repo", "path", fallback=""), "~/budge")
    ini.set("repo", "path", repo)

    ai_key = _collect_ai(cfg, ini)
    _collect_schedule(ini)

    header("interactive UIs (any combination, or none)")
    if not ini.has_section("ui"):
        ini.add_section("ui")
    current_ui = {u.strip() for u in
                  ini.get("ui", "enabled", fallback="paisa").split(",")
                  if u.strip()}
    chosen = choose_multi("Which UIs do you want on this machine?",
                          UI_OPTIONS, current_ui)
    ini.set("ui", "enabled", ",".join(sorted(chosen)))

    header("notifications (optional)")
    say("budge can send failure alerts and the weekly “review ready” nudge\n"
        "to OpenClaw — or to anything that accepts a JSON POST (an ntfy\n"
        "topic, a Slack/Discord webhook, ...). Paste that webhook URL, or\n"
        "leave blank to skip; you can add it any time under [notify] in\n"
        f"{conf_path()}.")
    stored_url = ini.get("notify", "openclaw_url", fallback="")
    ini.set("notify", "openclaw_url",
            _prompt_keep("notification webhook URL (blank to skip)",
                         stored_url))
    current_day = ini.get("notify", "review_day", fallback="Sat")
    days = _mark_current(
        [("Mon", "Monday"), ("Tue", "Tuesday"), ("Wed", "Wednesday"),
         ("Thu", "Thursday"), ("Fri", "Friday"), ("Sat", "Saturday"),
         ("Sun", "Sunday")], current_day)
    default = next((i for i, (v, _) in enumerate(days)
                    if v == current_day), 5)
    ini.set("notify", "review_day",
            choose("Which day do you want to do your ~10-minute weekly "
                   "review? (the nudge fires that morning)", days,
                   default=default))

    if not dry(f"write {conf_path()}"):
        with open(conf_path(), "w", encoding="utf-8") as f:
            ini.write(f)
        say(f"wrote {conf_path()}")
    return Config(), ai_key


def _collect_secrets(cfg, ai_key: str = "") -> Config:
    header("secrets — stored OUTSIDE the repo, chmod 600")
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

    if ai_key:
        secrets["AI_API_KEY"] = ai_key

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
    header("account mapping")
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
        kind = choose("  what kind of account is this?", [
            ("asset", "checking / savings (asset)"),
            ("liability", "credit card / line of credit (liability)"),
            ("invest", "investment / brokerage / stock plan (market value "
                       "changes without transactions)"),
        ])
        prefix = "liabilities:" if kind == "liability" else "assets:"
        hl_account = prompt("  hledger account name", prefix + slug)
        accounts.append({
            "id": sf["id"], "name": f"{org} {name}".strip(),
            "slug": slug, "account": hl_account, "currency": "$",
            "drift": kind == "invest",
        })
        declare_account(repo, hl_account)
        if not dry(f"seed rules file for {slug}"):
            seed_account_rules(repo, accounts[-1])
    if not dry("write import/accounts.json"):
        save_accounts(repo, accounts)
    commit_all(repo, "budge setup: account mapping + seeded transfer rules")
    say(f"mapped {len(accounts)} accounts; "
        "transfer/payment rule patterns seeded per account (PRD 7.3)")


def normalize_github_remote(url: str, method: str) -> str:
    """Accept owner/name or any GitHub URL form; emit the chosen transport."""
    import re
    m = re.search(
        r"(?:github\.com[:/])?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    if not m:
        return url  # not GitHub-shaped; pass through untouched
    owner, name = m.group(1), m.group(2)
    if method == "ssh":
        return f"git@github.com:{owner}/{name}.git"
    return f"https://github.com/{owner}/{name}.git"


def _github_remote(cfg) -> None:
    repo = cfg.repo
    current = git(repo, "remote", "get-url", "origin",
                  check=False).stdout.strip()
    if current:
        say(f"\ngit remote already configured: {current}")
        if not confirm("change it?", default=False):
            return
    url = prompt("\nGitHub repo for the journal (owner/name or URL — make "
                 "it PRIVATE; blank to skip)", current)
    if not url:
        return
    method = choose("How should this machine authenticate pushes?", [
        ("ssh", "SSH — a key in ~/.ssh added to GitHub (recommended for "
                "unattended timers)"),
        ("https", "HTTPS — requires a credential helper or personal "
                  "access token"),
    ])
    final = normalize_github_remote(url, method)
    if dry(f"git remote set origin {final}"):
        return
    if current:
        git(repo, "remote", "set-url", "origin", final)
    else:
        git(repo, "remote", "add", "origin", final)
    say(f"remote set: {final}")
    if method == "ssh":
        proc = run(["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new",
                    "git@github.com"], check=False)
        out = proc.stdout + proc.stderr
        if "successfully authenticated" in out:
            success("SSH authentication to GitHub works")
        else:
            warn("SSH to GitHub not working yet — generate a key with "
                 "`ssh-keygen -t ed25519` and add the .pub as a deploy key "
                 "(with write access) on the repo, then test with: "
                 "ssh -T git@github.com")


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
    header("90-day backfill — opening balances + balance assertions")
    run_fetch(cfg, backfill_days=90, interactive=True)


def _render_units(cfg) -> Path:
    """Render unit templates with concrete paths into <repo>/systemd/."""
    import budge
    # Templates ship INSIDE the package (budge/systemd/) so they exist in
    # pipx/pip installs, not just source checkouts.
    src = Path(budge.__file__).resolve().parent / "systemd"
    dst = cfg.repo / "systemd"
    if dry(f"render systemd unit templates into {dst}"):
        return dst
    dst.mkdir(exist_ok=True)
    budge_bin = shutil.which("budge") or "/usr/local/bin/budge"
    subs = {
        "@BUDGE@": budge_bin,
        "@HLEDGER_WEB@": shutil.which("hledger-web")
        or "/usr/bin/hledger-web",
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
    ui = cfg.ui_enabled
    units = list(rendered.glob("budge-*")) \
        + list(rendered.glob("paisa.service")) \
        + list(rendered.glob("hledger-web.service"))
    if not units:
        warn("nothing to install — no rendered units found in "
             f"{rendered} (was the render step skipped?)")
        return
    if os.access(target, os.W_OK):
        for unit in units:
            shutil.copy(unit, target / unit.name)
        run(["systemctl", "daemon-reload"], check=False)
        for timer in ["budge-fetch.timer", "budge-categorize.timer",
                      "budge-push.timer", "budge-review-nudge.timer"]:
            run(["systemctl", "enable", "--now", timer], check=False)
        say("systemd timers installed and enabled")
        _toggle_service(
            "paisa.service", "paisa" in ui
            and bool(shutil.which("podman") or shutil.which("docker")))
        _toggle_service(
            "hledger-web.service", "hledger-web" in ui
            and bool(shutil.which("hledger-web")))
    else:
        say(
            "\nTo install the timers + chosen UIs (needs root):\n"
            f"  sudo cp {rendered}/budge-* {rendered}/*.service "
            "/etc/systemd/system/\n"
            "  sudo systemctl daemon-reload\n"
            "  sudo systemctl enable --now budge-fetch.timer "
            "budge-categorize.timer budge-push.timer "
            "budge-review-nudge.timer"
            + "".join(f" {s}.service" for s in
                      ("paisa", "hledger-web") if s in ui)
        )


def _toggle_service(unit: str, wanted: bool) -> None:
    """Idempotently converge a service on the operator's UI choice."""
    if wanted:
        run(["systemctl", "reset-failed", unit], check=False)
        run(["systemctl", "enable", "--now", unit], check=False)
        run(["systemctl", "restart", unit], check=False)
        say(f"{unit}: enabled and (re)started")
    else:
        run(["systemctl", "disable", "--now", unit], check=False)


def _apt_install(package: str) -> None:
    """Best-effort install of a missing UI tool (root + apt only) — and say
    so plainly when it does not work."""
    if os.geteuid() != 0 or not shutil.which("apt-get"):
        return
    if dry(f"apt-get install {package}"):
        return
    say(f"installing {package}...")
    proc = run(["apt-get", "install", "-y", "-qq", package], check=False)
    if proc.returncode != 0:
        warn(f"apt-get install {package} failed: "
             + (proc.stderr.strip().splitlines() or ["no error output"])[-1])
    elif not shutil.which(package):
        warn(f"apt reported success but {package} is still not on PATH "
             f"(PATH={os.environ.get('PATH', '')})")


def _ui_extras(cfg) -> None:
    """Install whatever the operator chose, where budge can do it itself."""
    ui = cfg.ui_enabled
    if "hledger-ui" in ui and not shutil.which("hledger-ui"):
        _apt_install("hledger-ui")
    if "hledger-web" in ui and not shutil.which("hledger-web"):
        _apt_install("hledger-web")
    if "hledger-textual" in ui and not shutil.which("hledger-textual") \
            and shutil.which("pipx") and not dry("pipx install "
                                                 "hledger-textual"):
        say("installing hledger-textual via pipx...")
        run(["pipx", "install", "hledger-textual"], check=False)


def _service_state(unit: str) -> str:
    try:
        return run(["systemctl", "is-active", unit],
                   check=False).stdout.strip() or "unknown"
    except Exception:
        return "unknown"  # no systemd here (e.g. tests)


def _host_address() -> str:
    try:
        addrs = run(["hostname", "-I"], check=False).stdout.split()
        if addrs:
            return addrs[0]
    except Exception:
        pass
    return "<this-host>"


def _ui_status(cfg) -> None:
    """One unmissable block: each chosen UI, VERIFIED ready or what to do."""
    ui = cfg.ui_enabled
    if not ui:
        return
    header("UI status")
    host = _host_address()
    rows = []

    def service_row(name, unit, port, extra, install_fix):
        binary_ok = (bool(shutil.which("podman") or shutil.which("docker"))
                     if name == "paisa"
                     else bool(shutil.which(name)))
        if not binary_ok:
            return (name, False, "", install_fix)
        state = _service_state(unit)
        if state in ("active", "unknown"):
            return (name, True, f"http://{host}:{port}  {extra}", "")
        return (name, False, "",
                f"service is {state} — check: journalctl -u {unit} -n 20")

    if "paisa" in ui:
        rows.append(service_row(
            "paisa", "paisa.service", 7500, "(no auth — keep LAN-only)",
            "install podman, then re-run `budge ui`"))
    if "hledger-web" in ui:
        rows.append(service_row(
            "hledger-web", "hledger-web.service", 5000,
            "(view-only, no auth)", "apt install hledger-web && budge ui"))
    if "hledger-ui" in ui:
        rows.append(("hledger-ui", bool(shutil.which("hledger-ui")),
                     "run: hledger-ui", "apt install hledger-ui && budge ui"))
    if "hledger-textual" in ui:
        ok_bin = bool(shutil.which("hledger-textual"))
        ok_ver = _hledger_version(cfg) >= (1, 40)
        if ok_bin and ok_ver:
            rows.append(("hledger-textual", True,
                         "run: hledger-textual  "
                         "(edits txns — NEVER pending.journal, it's "
                         "derived; use budge review)", ""))
        elif not ok_ver:
            rows.append(("hledger-textual", False, "",
                         "needs hledger >= 1.40 — official binaries from "
                         "https://hledger.org/install into /usr/local/bin, "
                         "then `budge ui`"))
        else:
            rows.append(("hledger-textual", False, "",
                         "pipx install hledger-textual && budge ui"))
    for name, ready, how, fix in rows:
        if ready:
            say("  " + paint("✓", "green", "bold")
                + f" {name:<17s}" + how)
        else:
            say("  " + paint("✗", "red", "bold")
                + f" {name:<17s}"
                + paint("ACTION NEEDED: ", "red", "bold") + fix)


def _paisa(cfg) -> None:
    repo = cfg.repo
    if "paisa" not in cfg.ui_enabled:
        return
    paisa_yaml = repo / "paisa.yaml"
    content = (
        "# paisa.yaml — stock Paisa dashboard config (no modifications\n"
        "# to Paisa itself; it simply reads the hledger journal).\n"
        "# Paths are CONTAINER paths: paisa.service mounts the data repo\n"
        "# at /root/Documents/paisa, the location the Paisa image uses.\n"
        "# (Running a native paisa binary instead? Point these at the\n"
        "# host paths.)\n"
        "journal_path: /root/Documents/paisa/main.journal\n"
        "db_path: /root/Documents/paisa/paisa.db\n"
        "ledger_cli: hledger\n"
        "default_currency: USD\n"
        "locale: en-US\n"
    )
    if paisa_yaml.exists() and paisa_yaml.read_text(
            encoding="utf-8") != content:
        if not dry(f"update {paisa_yaml}"):
            paisa_yaml.write_text(content, encoding="utf-8")
            say(f"updated {paisa_yaml}")
    elif not paisa_yaml.exists() and not dry(f"write {paisa_yaml}"):
        paisa_yaml.write_text(content, encoding="utf-8")
        say(f"wrote {paisa_yaml}")
    say("Paisa dashboard: http://<this-host>:7500 once paisa.service is "
        "running (no auth built in — keep it LAN-only or front it with "
        "your own proxy).")


def _configure_ledger_file(cfg) -> None:
    """Point hledger at the journal for all interactive shells.

    With LEDGER_FILE set, the operator types `hledger register ...` from any
    directory — the journal location is configured once, at setup.
    """
    lines = [f'export LEDGER_FILE="{cfg.repo / "main.journal"}"']
    shell_path = os.environ.get("BUDGE_ORIG_PATH",
                                os.environ.get("PATH", ""))
    if "/usr/local/bin" not in shell_path.split(":"):
        # some LXC consoles omit it; manually-installed tools (newer
        # hledger, etc.) live there and should win over apt's versions
        lines.append('export PATH="/usr/local/bin:$PATH"')
    if "UTF-8" not in (os.environ.get("LC_ALL")
                       or os.environ.get("LANG") or ""):
        # hledger needs a UTF-8 locale to read UTF-8 journals; minimal LXC
        # consoles often have none configured.
        lines.append('export LC_ALL="${LC_ALL:-C.UTF-8}"')
    rc = Path.home() / ".bashrc"
    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    if all(l in existing for l in lines):
        return
    if dry(f"set LEDGER_FILE (and locale if needed) in {rc}"):
        return
    cleaned = "\n".join(
        l for l in existing.splitlines()
        if "LEDGER_FILE" not in l or not l.strip().startswith("export")
    )
    with open(rc, "w", encoding="utf-8") as f:
        f.write(cleaned.rstrip("\n")
                + "\n\n# budge: hledger journal location (managed by "
                  "budge setup)\n" + "\n".join(lines) + "\n")
    say(f"LEDGER_FILE set in {rc} — plain `hledger` commands work from "
        "anywhere (open a new shell or `source ~/.bashrc`)")


def run_ui(cfg, show_only: bool = False) -> None:
    """`budge ui` — choose interactive UIs without the full setup flow."""
    current = cfg.ui_enabled
    if show_only:
        for value, label in UI_OPTIONS:
            mark = paint("[x]", "green", "bold") if value in current \
                else "[ ]"
            say(f"  {mark} {label}")
        return
    header("interactive UIs (any combination, or none)")
    chosen = choose_multi("Which UIs do you want on this machine?",
                          UI_OPTIONS, current)
    ini = configparser.ConfigParser()
    if conf_path().exists():
        ini.read(conf_path(), encoding="utf-8")
    if not ini.has_section("ui"):
        ini.add_section("ui")
    ini.set("ui", "enabled", ",".join(sorted(chosen)))
    if not dry(f"write {conf_path()}"):
        with open(conf_path(), "w", encoding="utf-8") as f:
            ini.write(f)
    cfg = Config()  # reload with the new choices
    _ui_extras(cfg)
    rendered = _render_units(cfg)
    _paisa(cfg)
    _install_units(cfg, rendered)
    _ui_status(cfg)
    _configure_ledger_file(cfg)  # keeps PATH/locale/LEDGER_FILE current too


def run_setup(cfg, services_only: bool = False) -> None:
    if services_only:
        # Re-render and (re)install systemd units + the Paisa dashboard from
        # existing configuration — no prompts, no wizard, no data changes.
        banner("setup --services-only — timers + dashboard from existing "
               "config")
        _ui_extras(cfg)
        rendered = _render_units(cfg)
        _paisa(cfg)
        _install_units(cfg, rendered)
        _ui_status(cfg)
        _configure_ledger_file(cfg)
        return
    banner("setup — safe to re-run; Enter keeps any existing value")
    _check_prereqs(cfg)
    cfg, ai_key = _collect_config(cfg)
    cfg = _collect_secrets(cfg, ai_key)
    scaffold(cfg.repo)
    _map_accounts(cfg)
    _github_remote(cfg)
    _backfill(cfg)
    _ui_extras(cfg)
    rendered = _render_units(cfg)
    _paisa(cfg)
    _install_units(cfg, rendered)
    _ui_status(cfg)
    _configure_ledger_file(cfg)
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

    header("what happens next")
    say(
        ""
        f"  every morning  budge-fetch pulls new transactions into "
        f"{cfg.repo / 'main.journal'}\n"
        "                 (rule-matched: cleared; the rest: pending via AI)\n"
        "  after fetch    budge-categorize suggests categories for the rest\n"
        "  after both     budge-push syncs the repo to your remote\n"
        "  weekly         OpenClaw nudges you; run `budge review` "
        "(~10 minutes)\n"
        "  anytime        the Paisa dashboard answers “how much is left?”\n"
        "  reporting      plain hledger from any directory, e.g. "
        "hledger balance --budget -M expenses\n"
    )
