"""Review & promote CLI (PRD section 7.5) — `budge review`.

Vendor-grouped weekly review. Per group: approve, correct the vendor (writes a
rule + regenerates pending), or correct a single transaction (one-off, logged
as a manual override so it survives regeneration). Promote is the explicit
final step, hard-gated by `hledger check`.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections import OrderedDict
from pathlib import Path

from . import ailog, categorize, hledger, journal
from .gitutil import commit_all, push
from .scaffold import (add_vendor_rule, declare_account, load_accounts,
                       payee_pattern)
from .util import confirm, die, dry, edit_text, prompt, say, warn


def _groups(entries: list) -> "OrderedDict":
    """payee -> entries, sorted by count desc (day-one backfill friendly)."""
    by_payee = {}
    for e in entries:
        by_payee.setdefault(e.payee, []).append(e)
    return OrderedDict(
        sorted(by_payee.items(), key=lambda kv: -len(kv[1]))
    )


def _amount_total(entries: list) -> str:
    total = 0.0
    for e in entries:
        m = re.search(r"-?[\d,]+\.?\d*", e.amount.replace("$", ""))
        if m:
            total += float(m.group(0).replace(",", ""))
    return f"{total:,.2f}"


def _lowest_confidence(entries: list) -> str:
    order = {"": 0, "low": 1, "medium": 2, "high": 3}
    return min((e.confidence for e in entries), key=lambda c: order.get(c, 0))


def _slug_for(entries: list, repo: Path) -> list:
    """Rules-file slugs whose accounts appear in these entries."""
    accounts = load_accounts(repo)
    by_account = {a["account"]: a["slug"] for a in accounts}
    return sorted({
        by_account[e.source_account]
        for e in entries if e.source_account in by_account
    })


def _transfers_warning(repo: Path) -> None:
    bal = hledger.account_balance(repo / "main.journal", journal.TRANSFERS)
    if bal and not re.fullmatch(r"\$?0(\.0+)?", bal.replace(",", "")):
        warn(
            f"assets:transfers does not net to zero (balance: {bal}). "
            "One side of a transfer/card payment is missing or miscoded — "
            "check with: hledger -f main.journal register assets:transfers"
        )


def _prompt_category(repo: Path, default: str = "") -> str:
    cats = sorted(categorize.allowed_categories(repo))
    while True:
        cat = prompt("category", default).strip()
        if not cat:
            return ""
        if cat in cats:
            return cat
        if confirm(f"{cat!r} is not declared in accounts.journal — "
                   f"declare it and use it?"):
            declare_account(repo, cat)
            return cat
        say("known categories: " + ", ".join(
            c for c in cats if c.startswith("expenses") or
            c.startswith("income")))


def correct_vendor(cfg, payee: str, entries: list, category: str) -> None:
    """Write/update a rule, then regenerate pending (acceptance A7).

    The rule edit is validated by regeneration's hledger check gate; on
    failure the rules file is rolled back and nothing else changes.
    """
    repo = cfg.repo
    pattern = payee_pattern(payee)
    backups = {}
    for slug in _slug_for(entries, repo) or [a["slug"] for a in
                                             load_accounts(repo)]:
        backups[slug] = add_vendor_rule(repo, slug, pattern, category)
    declare_account(repo, category)
    try:
        result = categorize.regenerate(cfg)
    except SystemExit:
        for slug, old in backups.items():
            (repo / "import" / "rules" / f"{slug}.rules").write_text(
                old, encoding="utf-8")
        warn("rule rolled back — hledger rejected the result")
        raise
    say(f"rule written ({pattern} -> {category}); "
        f"{result['promoted_by_rule']} matching txns moved to main.journal "
        f"as cleared; {result['kept']} remain pending")


def correct_single(cfg, entry, category: str) -> None:
    """One-off correction: no rule. Logged so regeneration preserves it."""
    repo = cfg.repo
    declare_account(repo, category)
    ailog.append(repo, event="manual_override", txn_id=entry.sf_id,
                 payee=entry.payee, category=category)
    entries = journal.parse_pending(repo / "pending.journal")
    for e in entries:
        if e.sf_id == entry.sf_id:
            if e.suggested and e.suggested != category:
                pass  # outcome recorded at promote
            e.category = category
            e.origin = "manual"
    journal.write_pending(repo / "pending.journal", entries)


def promote(cfg) -> bool:
    """Explicit final step (PRD 7.5). Order is load-bearing:

    1. `hledger check` on the books AS THEY ARE (catches malformed pending —
       acceptance A8) — any failure stops everything, nothing written.
    2. Rehearse the flip in a temp copy of the repo; `hledger check` again.
    3. Only then: append cleared entries to main.journal, truncate
       pending.journal, append outcome events to the decision log,
       ONE commit (journal + rules together), push.
    """
    repo = cfg.repo
    ok, output = hledger.check(repo / "main.journal")
    if not ok:
        die("PROMOTE HALTED — hledger check failed; nothing was written, "
            "committed, or pushed:\n" + output)

    entries = journal.parse_pending(repo / "pending.journal")
    if not entries:
        say("pending.journal is empty — nothing to promote")
        return False

    # Guard against silent data loss from hand-edits our parser missed:
    proc = hledger.hledger(["-f", repo / "pending.journal", "print"],
                           check=False)
    hl_count = len(hledger.split_entries(proc.stdout))
    if hl_count != len(entries):
        die(f"PROMOTE HALTED — pending.journal contains {hl_count} entries "
            f"per hledger but budge parsed {len(entries)}. A hand-edited "
            "entry is in a shape budge does not understand; fix it or "
            "regenerate (budge regenerate).")

    cleared = [journal.render_cleared(e) for e in entries]

    # Rehearsal in a temp copy (steps so a failure writes nothing — A8).
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "repo"
        shutil.copytree(repo, tmp, ignore=shutil.ignore_patterns(".git"))
        with open(tmp / "main.journal", "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(cleared))
        (tmp / "pending.journal").write_text(journal.PENDING_HEADER,
                                             encoding="utf-8")
        ok, output = hledger.check(tmp / "main.journal")
        if not ok:
            die("PROMOTE HALTED — flipped entries fail hledger check; "
                "nothing was written, committed, or pushed:\n" + output)

    if dry(f"promote {len(entries)} pending entries to main.journal"):
        return False

    with open(repo / "main.journal", "a", encoding="utf-8") as f:
        f.write("\n" + "\n".join(cleared))
    (repo / "pending.journal").write_text(journal.PENDING_HEADER,
                                          encoding="utf-8")
    for e in entries:
        if e.suggested:
            ailog.append(
                repo, event="outcome", txn_id=e.sf_id,
                suggestion=e.suggested, final=e.category,
                result="accepted" if e.category == e.suggested
                else "overridden",
            )
    commit_all(repo, f"budge promote: {len(entries)} reviewed transactions")
    push(repo)
    say(f"promoted {len(entries)} transactions; committed and pushed")
    return True


def run_review(cfg, edit: bool = False) -> None:
    repo = cfg.repo
    if edit:
        # Escape hatch: $EDITOR on pending.journal (PRD 7.5)
        path = repo / "pending.journal"
        new = edit_text(path.read_text(encoding="utf-8"), suffix=".journal")
        from .util import write_file
        write_file(path, new)
        ok, output = hledger.check(repo / "main.journal")
        if not ok:
            die("your edit fails hledger check (fix and re-run):\n" + output)
        say("pending.journal updated")
        return

    _transfers_warning(repo)
    entries = journal.parse_pending(repo / "pending.journal")
    if not entries:
        say("pending.journal is empty — nothing to review")
        return

    gaps = [e for e in entries if e.category == journal.TRANSFERS]
    if gaps:
        warn(f"{len(gaps)} transfer-shaped txns reached the AI (rules gap) — "
             "approve them and consider a rules pattern: "
             + ", ".join(sorted({e.payee for e in gaps})[:5]))

    handled = set()
    while True:
        entries = journal.parse_pending(repo / "pending.journal")
        groups = _groups(entries)
        remaining = [p for p in groups if p not in handled]
        say(f"\n{len(entries)} pending in {len(groups)} vendor groups "
            f"({len(remaining)} unhandled)")
        if not remaining:
            break
        payee = remaining[0]
        group = groups[payee]
        low = _lowest_confidence(group)
        say(f"\n--- {payee} ---")
        say(f"  {len(group)} txns, total {_amount_total(group)}, "
            f"suggested {group[0].category}"
            + (f", lowest confidence {low}" if low else ", no AI suggestion"))
        for e in group[:8]:
            say(f"    {e.date}  {e.amount:>12}  {e.category}"
                + (f"  ({e.confidence})" if e.confidence else ""))
        if len(group) > 8:
            say(f"    ... and {len(group) - 8} more")
        action = prompt(
            "[a]pprove  [v]endor rule  [s]ingle txn  [k]skip  [q]uit",
            "a").lower()
        if action == "q":
            break
        if action == "k":
            handled.add(payee)
            continue
        if action == "a":
            handled.add(payee)
            continue
        if action == "v":
            cat = _prompt_category(repo, group[0].category)
            if cat:
                correct_vendor(cfg, payee, group, cat)
                handled.add(payee)
            continue
        if action == "s":
            for i, e in enumerate(group):
                say(f"  [{i}] {e.date} {e.amount:>12} {e.category}")
            try:
                idx = int(prompt("which", "0"))
                target = group[idx]
            except (ValueError, IndexError):
                warn("no such transaction")
                continue
            cat = _prompt_category(repo, target.category)
            if cat:
                correct_single(cfg, target, cat)
            continue

    entries = journal.parse_pending(repo / "pending.journal")
    if not entries:
        say("\nnothing left pending")
        return
    unhandled = [p for p in _groups(entries) if p not in handled]
    if unhandled:
        warn(f"{len(unhandled)} vendor groups were not reviewed")
    if confirm(f"\nPromote all {len(entries)} pending transactions "
               "(hledger check gate, then commit + push)?", default=False):
        promote(cfg)
    else:
        say("not promoted — pending stays visible to the budget; "
            "run `budge review` again when ready")
