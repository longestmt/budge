"""Fetch & import pipeline (PRD section 7.2) + 90-day backfill (section 7.1).

Flow per run:
  SimpleFIN pull -> append new rows to immutable monthly raw CSVs
  -> convert new rows through the account's hledger rules file
  -> rule-matched entries  -> main.journal (cleared *)   [never reviewed]
  -> unmatched entries     -> pending.journal (status !, expenses:uncategorized)
  -> balance assertion per account -> hledger check -> single git commit

Dedup: SimpleFIN transaction IDs recorded in import/state/<slug>.ids (the
dedup state the PRD locates in import/state/), PLUS a belt-and-suspenders
scan of simplefin_id tags already present in main/pending. A transaction can
never import twice, across re-runs and backfills.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import tempfile
import time
from pathlib import Path

from . import hledger, journal, simplefin
from .gitutil import commit_all
from .scaffold import declare_account, load_accounts
from .util import confirm, die, dry, say, warn

CSV_FIELDS = ["id", "date", "amount", "payee", "memo"]


# ---------------------------------------------------------------- state ----

def _state_path(repo: Path, slug: str) -> Path:
    return Path(repo) / "import" / "state" / f"{slug}.ids"


def seen_ids(repo: Path, slug: str) -> set:
    path = _state_path(repo, slug)
    if not path.exists():
        return set()
    return set(path.read_text(encoding="utf-8").split())


def record_ids(repo: Path, slug: str, ids: list) -> None:
    if not ids or dry(f"record {len(ids)} txn ids in state/{slug}.ids"):
        return
    path = _state_path(repo, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(ids) + "\n")


# ------------------------------------------------------------- raw CSVs ----

def _month_csv(repo: Path, slug: str, yyyymm: str) -> Path:
    return Path(repo) / "import" / "raw" / yyyymm / f"{slug}.csv"


def append_raw_rows(repo: Path, slug: str, rows: list) -> list:
    """Append rows (sorted by date) to per-month CSVs. Returns paths touched.

    Existing rows are never modified — the raw CSVs are an immutable record
    of what SimpleFIN returned.
    """
    touched = []
    by_month = {}
    for row in sorted(rows, key=lambda r: r["date"]):
        by_month.setdefault(row["date"][:7], []).append(row)
    for month, month_rows in by_month.items():
        path = _month_csv(repo, slug, month)
        if dry(f"append {len(month_rows)} rows to {path}"):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if new_file:
                writer.writeheader()
            for row in month_rows:
                writer.writerow({k: row[k] for k in CSV_FIELDS})
        touched.append(path)
    return touched


def all_raw_rows(repo: Path) -> dict:
    """txn id -> (slug, row) across every committed raw CSV."""
    out = {}
    raw = Path(repo) / "import" / "raw"
    if not raw.exists():
        return out
    for path in sorted(raw.glob("*/*.csv")):
        slug = path.stem
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["id"]] = (slug, row)
    return out


# ----------------------------------------------------------- conversion ----

def convert_rows(repo: Path, slug: str, rows: list) -> dict:
    """Run rows through the account's hledger rules file.

    Returns {txn_id: entry_text}. Pairing is by the simplefin_id tag the rules
    file stamps into each entry (hledger print may reorder by date).
    """
    if not rows:
        return {}
    rules = Path(repo) / "import" / "rules" / f"{slug}.rules"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row[k] for k in CSV_FIELDS})
    with tempfile.NamedTemporaryFile(
        "w", suffix=".csv", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(buf.getvalue())
        tmp = Path(tf.name)
    try:
        entries = hledger.csv_to_entries(tmp, rules)
    finally:
        tmp.unlink(missing_ok=True)
    out = {}
    for entry in entries:
        m = re.search(r"simplefin_id:\s*(\S+)", entry)
        if m:
            out[m.group(1)] = entry
    return out


def entry_category(entry_text: str, source_account: str) -> str:
    """The non-source posting account of a converted entry (account2)."""
    for line in entry_text.splitlines():
        s = line.strip()
        if s and not s.startswith(";") and not re.match(r"^\d{4}", s):
            pm = re.match(r"^(\S[\S ]*?)(?:\s{2,}.*)?$", s)
            if pm:
                account = pm.group(1).strip()
                if account != source_account:
                    return account
    return ""


def _amount_str(amount: str, currency: str) -> str:
    amount = str(amount).strip()
    if currency == "$":
        return ("$-" + amount[1:]) if amount.startswith("-") else ("$" + amount)
    return f"{amount} {currency}"


# ------------------------------------------------------------- pipeline ----

def sf_rows(sf_account: dict) -> list:
    """SimpleFIN account JSON -> canonical CSV rows."""
    rows = []
    for t in sf_account.get("transactions", []):
        posted = t.get("posted") or t.get("transacted_at") or 0
        date = dt.datetime.fromtimestamp(int(posted)).strftime("%Y-%m-%d")
        payee = (t.get("payee") or t.get("description") or "UNKNOWN").strip()
        memo = (t.get("memo") or t.get("description") or "").strip()
        rows.append({
            "id": str(t["id"]),
            "date": date,
            "amount": str(t["amount"]),
            "payee": payee,
            "memo": memo if memo != payee else "",
        })
    return rows


def _ledger_balance(repo: Path, account: str) -> float:
    """Current journal balance of an account as a float (0.0 if none)."""
    raw = hledger.account_balance(Path(repo) / "main.journal", account)
    m = re.search(r"-?[\d,]+\.?\d*", raw.replace("$", ""))
    return float(m.group(0).replace(",", "")) if m else 0.0


def market_adjustment_entry(date: str, account: str, diff: float,
                            currency: str) -> str:
    """Unrealized gain/loss posting for accounts whose market value moves
    without transactions (brokerage, stock plans). Keeps the daily balance
    assertion truthful instead of failing on every market move."""
    return (
        f"{date} * market value adjustment (simplefin)\n"
        f"    ; unrealized gain/loss reconciling reported market value\n"
        f"    {account:<40s}  {_amount_str(f'{diff:.2f}', currency)}\n"
        f"    equity:unrealized-gains\n"
    )


def assertion_entry(date: str, account: str, balance: str, currency: str,
                    note: str = "") -> str:
    bal = _amount_str(balance, currency)
    comment = f"    ; {note}\n" if note else ""
    return (
        f"{date} * balance assertion (simplefin)\n"
        f"{comment}"
        f"    {account:<40s}  {('$0' if currency == '$' else '0 ' + currency)}"
        f" = {bal}\n"
    )


def _remove_old_assertion(repo: Path, account: str) -> None:
    """Drop budge's previous running assertion for this account.

    A pinned historical assertion breaks legitimately when a late-posting
    transaction (dated before the assertion) arrives in a later feed pull.
    budge therefore maintains exactly ONE current assertion per account —
    yesterday's mark is superseded, and git history preserves every old one.
    """
    if dry(f"supersede previous balance assertion for {account}"):
        return
    main = Path(repo) / "main.journal"
    text = main.read_text(encoding="utf-8")
    pattern = re.compile(
        r"\d{4}-\d{2}-\d{2} \* balance assertion \(simplefin\)\n"
        r"(?:[ \t]+;[^\n]*\n)*"
        rf"[ \t]+{re.escape(account)}[ \t][^\n]*\n",
    )
    new = pattern.sub("", text)
    if new != text:
        new = re.sub(r"\n{3,}", "\n\n", new)
        main.write_text(new, encoding="utf-8")


def append_main(repo: Path, texts: list) -> None:
    if not texts:
        return
    from .util import append_file
    append_file(Path(repo) / "main.journal",
                "\n" + "\n\n".join(t.rstrip("\n") for t in texts) + "\n")


def run_fetch(cfg, backfill_days: int = None, interactive: bool = False):
    """The daily pipeline. With backfill_days set, also writes opening
    balances (setup's 90-day backfill, PRD section 7.1)."""
    repo = cfg.repo
    accounts = load_accounts(repo)
    if not accounts:
        die("no accounts mapped yet — run `budge setup`")
    access_url = cfg.simplefin_access_url
    if not access_url:
        die("SIMPLEFIN_ACCESS_URL missing from secrets.env — run `budge setup`")

    days = backfill_days or 7  # daily runs re-query a week for stragglers
    start = int(time.time()) - days * 86400
    data = simplefin.get_accounts(access_url, start_date=start)
    sf_by_id = {a["id"]: a for a in data.get("accounts", [])}

    today = dt.date.today().isoformat()
    known = journal.journal_sf_ids(repo / "main.journal") \
        | journal.journal_sf_ids(repo / "pending.journal")
    pending_entries = journal.parse_pending(repo / "pending.journal")
    main_texts, summary = [], []

    for acct in accounts:
        sf = sf_by_id.get(acct["id"])
        if sf is None:
            warn(f"account {acct['slug']} not in SimpleFIN response; skipping")
            continue
        currency = acct.get("currency", "$")
        rows = sf_rows(sf)
        seen = seen_ids(repo, acct["slug"]) | known
        new = [r for r in rows if r["id"] not in seen]
        # drop duplicates inside one response, defensively
        uniq, new_rows = set(), []
        for r in new:
            if r["id"] not in uniq:
                uniq.add(r["id"])
                new_rows.append(r)

        append_raw_rows(repo, acct["slug"], new_rows)
        converted = convert_rows(repo, acct["slug"], new_rows)
        n_matched = n_pending = 0
        for row in new_rows:
            entry = converted.get(row["id"])
            category = entry_category(entry, acct["account"]) if entry else ""
            if entry and category and category != journal.UNCATEGORIZED:
                main_texts.append(entry)          # rule-matched, cleared *
                n_matched += 1
            else:
                pending_entries.append(journal.Pending(
                    date=row["date"], payee=row["payee"], sf_id=row["id"],
                    source_account=acct["account"],
                    amount=_amount_str(row["amount"], currency),
                    origin="uncategorized",
                ))
                n_pending += 1
        record_ids(repo, acct["slug"], [r["id"] for r in new_rows])

        if backfill_days and "balance" in sf:
            # Opening balance so that opening + transactions = reported
            # balance. Written even when the account has ZERO transactions
            # in the window (common for brokerage/stock-plan accounts) —
            # otherwise the assertion below is guaranteed to fail.
            already = acct["account"] in (
                (repo / "main.journal").read_text(encoding="utf-8"))
            total = sum(float(r["amount"]) for r in new_rows)
            reported = float(sf.get("balance", 0))
            opening = round(reported - total, 2)
            if abs(opening) >= 0.005 and not already:
                first = min((r["date"] for r in new_rows), default=None)
                open_date = (
                    dt.date.fromisoformat(first) - dt.timedelta(days=1)
                    if first else
                    dt.date.today() - dt.timedelta(days=backfill_days)
                ).isoformat()
                main_texts.insert(0, (
                    f"{open_date} * opening balances\n"
                    f"    ; computed by budge setup: reported {reported:.2f}"
                    f" - imported txns {total:.2f}\n"
                    f"    {acct['account']:<40s}  "
                    f"{_amount_str(f'{opening:.2f}', currency)}\n"
                    f"    {journal.OPENING}\n"
                ))
        if "balance" in sf:
            if acct.get("drift") and not backfill_days:
                # Market-valued account: reconcile value changes that have
                # no transactions behind them before asserting.
                ledger = _ledger_balance(repo, acct["account"]) \
                    + sum(float(r["amount"]) for r in new_rows)
                diff = round(float(sf["balance"]) - ledger, 2)
                if abs(diff) >= 0.01:
                    declare_account(repo, "equity:unrealized-gains")
                    main_texts.append(market_adjustment_entry(
                        today, acct["account"], diff, currency))
            _remove_old_assertion(repo, acct["account"])
            main_texts.append(assertion_entry(
                today, acct["account"], str(sf["balance"]), currency))
        summary.append(
            f"{acct['slug']}: {len(new_rows)} new "
            f"({n_matched} rule-matched, {n_pending} to pending)")

    append_main(repo, main_texts)
    journal.write_pending(repo / "pending.journal", pending_entries)

    ok, output = hledger.check(repo / "main.journal")
    if not ok:
        if interactive and backfill_days:
            _discrepancy_flow(cfg, output)
        else:
            # tolerant alerting language (PRD section 10): often a bank-pending
            # transaction counted in the balance but absent from the feed.
            die(
                "hledger check failed after import — continuous reconciliation "
                "tripped. This is often a bank-pending transaction that is "
                "included in the reported balance but not yet in the "
                "transaction feed; it may clear by tomorrow's run.\n\n" + output
            )

    commit_all(cfg.repo, f"budge fetch: {'; '.join(summary) or 'no new transactions'}")
    for line in summary:
        say(line)
    return summary


def _discrepancy_flow(cfg, check_output: str) -> None:
    """PRD section 7.1: never abort silently on a failed backfill assertion."""
    repo = cfg.repo
    say("\nA balance assertion failed during backfill. The arithmetic:")
    say(check_output)
    say(
        "\nLikely cause: a bank-pending transaction is included in the "
        "balance SimpleFIN reports but absent from the transaction list, "
        "so the imported history sums short of the reported balance."
    )
    if confirm("Adjust the assertion to the computed ledger balance and "
               "proceed? (No halts setup here)", default=True):
        _relax_assertions(repo, check_output)
        ok, output = hledger.check(repo / "main.journal")
        if not ok:
            die("still failing after adjustment — please inspect manually:\n"
                + output)
        say("assertion adjusted; a note records the bank-reported figure.")
    else:
        die("halted at your request — nothing has been committed")


def _relax_assertions(repo: Path, check_output: str) -> None:
    """Replace a failing asserted amount with the calculated one, keeping the
    bank-reported figure in a comment for the audit trail."""
    m = re.search(r"calculated:\s*(.+)", check_output)
    m2 = re.search(r"asserted:\s*(.+)", check_output)
    if not (m and m2):
        die("could not parse the assertion discrepancy:\n" + check_output)
    calculated = m.group(1).strip().rstrip(",")
    asserted = m2.group(1).strip().rstrip(",")
    main = Path(repo) / "main.journal"
    text = main.read_text(encoding="utf-8")
    needle = f"= {asserted}"
    if needle not in text:
        # hledger may render with/without commas; try a normalized form
        needle = "= " + asserted.replace(",", "")
    text = text.replace(
        needle,
        f"= {calculated}  ; adjusted: bank reported {asserted} "
        f"(pending-transaction discrepancy)",
        1,
    )
    main.write_text(text, encoding="utf-8")
