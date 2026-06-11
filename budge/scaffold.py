"""Data-repo scaffolding per PRD section 5. Idempotent: never overwrites."""

from __future__ import annotations

import json
from pathlib import Path

from .agent_template import AGENT_MD
from .gitutil import commit_all, ensure_repo
from .util import dry, say

MAIN_SKELETON = """\
; main.journal — household books. SOURCE OF TRUTH.
; The operator interface is hledger itself, e.g.:
;   hledger -f main.journal balance --budget
;   hledger -f main.journal register expenses:dining
; budge only ever APPENDS hledger-format text below the marker line.

include accounts.journal
include budget.journal
include pending.journal

; --- transactions (appended by budge fetch / promote) ---
"""

ACCOUNTS_SKELETON = """\
; accounts.journal — chart of accounts (declarations).
; Baseline accounts are seeded by setup; categories are proposed by
; `budge plan` and confirmed by the operator.

account assets:transfers          ; clearing account: card payments & transfers
account equity:opening-balances
account equity:unrealized-gains   ; market-value drift on investment accounts
account expenses:uncategorized    ; AI could not categorize; fix in review
"""

BUDGET_SKELETON = """\
; budget.journal — monthly envelopes as an hledger periodic transaction.
; Written by `budge plan` (with your confirmation). View with:
;   hledger -f main.journal balance --budget -M expenses
"""

PENDING_SKELETON = """\
; pending.journal — DERIVED FILE, managed by budge.
; AI-categorized transactions awaiting review (status !).
"""

HOUSEHOLD_SKELETON = """\
# household.md

Filled in by `budge plan` (income, savings target, goals). Edit freely —
budge reads it on every wizard run and appends each run's decisions below.
"""

GITIGNORE = """\
# defense in depth: secrets live in ~/.config/budge/, never here.
*.env
secrets*
*.key
*.pem
# hledger .latest state (budge uses import/state/ ID files instead)
.latest*
# editor litter
*~
*.swp
.#*
"""

# Transfer/payment patterns seeded into every account's rules file (PRD 7.3).
# These sit at the BOTTOM of the rules file so they take precedence over
# vendor rules (in hledger CSV rules, later matching blocks win).
TRANSFER_RULES = """\
# --- transfer & payment patterns (seeded by setup; PRD 7.3) -----------------
# Card payments and inter-account transfers post against the clearing account
# assets:transfers from BOTH feeds, so they net to zero and are never counted
# as spending. Keep this section LAST so it outranks vendor rules.
if
%payee PAYMENT THANK YOU
%payee AUTOPAY
%payee AUTOMATIC PAYMENT
%payee ONLINE TRANSFER
%payee TRANSFER (TO|FROM)
%payee ACH (PAYMENT|TRANSFER|WITHDRAWAL)
%payee ZELLE (TO|FROM)
%payee VENMO (PAYMENT|CASHOUT|TRANSFER)
%payee CARDMEMBER SERV
%payee INTERNET TRANSFER
%payee EPAY
%payee E-PAYMENT
%payee CREDIT CARD PMT
 account2 assets:transfers
"""

VENDOR_MARKER = "# --- vendor rules (managed by budge: wizard + review corrections) ---"
TRANSFER_MARKER = "# --- transfer & payment patterns"


def rules_template(slug: str, bank_name: str, hl_account: str,
                   currency: str = "$") -> str:
    return f"""\
# import/rules/{slug}.rules — hledger CSV rules for {bank_name}
# Stock hledger CSV rules syntax only:
#   https://hledger.org/hledger.html#csv
# budge appends vendor rules under the marker; transfer patterns stay last.

skip 1
fields id, date, amount, payee, memo
date-format %Y-%m-%d
currency {currency}
status *
account1 {hl_account}
description %payee
comment simplefin_id:%id
account2 expenses:uncategorized

{VENDOR_MARKER}

{TRANSFER_RULES}"""


def scaffold(repo: Path) -> None:
    """Create the PRD section-5 layout. Existing files are left untouched."""
    repo = Path(repo)
    if dry(f"scaffold data repo at {repo}"):
        return
    repo.mkdir(parents=True, exist_ok=True)
    dirs = ["import/rules", "import/state", "ai", "scripts", "systemd"]
    for d in dirs:
        (repo / d).mkdir(parents=True, exist_ok=True)
    seeds = {
        "main.journal": MAIN_SKELETON,
        "accounts.journal": ACCOUNTS_SKELETON,
        "budget.journal": BUDGET_SKELETON,
        "pending.journal": PENDING_SKELETON,
        "household.md": HOUSEHOLD_SKELETON,
        ".gitignore": GITIGNORE,
        "ai/agent.md": AGENT_MD,
    }
    created = []
    for rel, content in seeds.items():
        path = repo / rel
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            created.append(rel)
    ensure_repo(repo)
    if created:
        commit_all(repo, f"budge scaffold: {', '.join(created)}")
        say(f"scaffolded {repo} ({len(created)} files)")
    else:
        say(f"repo {repo} already scaffolded; nothing changed")


def load_accounts(repo: Path) -> list:
    """import/accounts.json: SimpleFIN account id -> hledger mapping."""
    path = Path(repo) / "import" / "accounts.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_accounts(repo: Path, accounts: list) -> None:
    path = Path(repo) / "import" / "accounts.json"
    path.write_text(json.dumps(accounts, indent=2) + "\n", encoding="utf-8")


def seed_account_rules(repo: Path, account: dict) -> None:
    """Write the rules file for one bank account if absent (idempotent)."""
    path = Path(repo) / "import" / "rules" / f"{account['slug']}.rules"
    if not path.exists():
        path.write_text(
            rules_template(
                account["slug"], account.get("name", account["slug"]),
                account["account"], account.get("currency", "$"),
            ),
            encoding="utf-8",
        )


def payee_pattern(payee: str) -> str:
    """A %payee matcher for one exact vendor string.

    hledger CSV `if` patterns are case-insensitive POSIX regexps; %payee
    scopes the match to the payee field so anchors behave.
    """
    import re as _re
    return "%payee ^" + _re.escape(payee) + "$"


def add_vendor_rule(repo: Path, slug: str, pattern: str, category: str) -> str:
    """Insert a vendor rule BEFORE the transfer section (transfers stay last).

    `pattern` is a full matcher line (see payee_pattern). Returns the previous
    file content so callers can roll back if the new rule fails the hledger
    check gate.
    """
    path = Path(repo) / "import" / "rules" / f"{slug}.rules"
    old = path.read_text(encoding="utf-8")
    block = f"if {pattern}\n account2 {category}\n\n"
    if block in old:
        return old  # idempotent: identical rule already present
    if dry(f"add rule to {path.name}: if {pattern} -> {category}"):
        return old
    idx = old.find(TRANSFER_MARKER)
    if idx == -1:
        new = old.rstrip("\n") + "\n\n" + block
    else:
        new = old[:idx] + block + old[idx:]
    path.write_text(new, encoding="utf-8")
    return old


def declare_account(repo: Path, account_name: str) -> bool:
    """Append an account declaration to accounts.journal if missing."""
    path = Path(repo) / "accounts.journal"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    declared = {
        line.split(";")[0].replace("account", "", 1).strip()
        for line in text.splitlines()
        if line.strip().startswith("account ")
    }
    if account_name in declared:
        return False
    if dry(f"declare account {account_name} in accounts.journal"):
        return False
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"account {account_name}\n")
    return True
