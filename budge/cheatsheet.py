"""`budge cheatsheet` — curated hledger recipes for the budge journal.

Prints commands; never runs them. The operator's accounting interface is
hledger itself (PRD section 1) — this exists to shorten the trip to
https://hledger.org/hledger.html. All examples assume LEDGER_FILE is set
(budge setup does this), so no -f flag is needed.
"""

from __future__ import annotations

from .util import header, note, paint, say

# (section title, [(what you want, the command), ...])
SECTIONS = [
    ("finding transactions", [
        ("all transactions for a vendor",
         "hledger register desc:KROGER"),
        ("all transactions in a category",
         "hledger register expenses:dining"),
        ("a category, including subcategories",
         "hledger register expenses:auto    # matches expenses:auto:fuel too"),
        ("transactions over a certain amount",
         "hledger register expenses 'amt:>100'"),
        ("an exact amount (e.g. hunting a charge)",
         "hledger register 'amt:14.99'"),
        ("vendor AND month AND amount together",
         "hledger register desc:AMAZON date:2026/05 'amt:>50'"),
        ("everything EXCEPT a category",
         "hledger register expenses not:expenses:housing"),
        ("full entries instead of one-line register",
         "hledger print desc:NETFLIX"),
    ]),
    ("time windows", [
        ("a specific month",
         "hledger register expenses:dining -p 2026-05"),
        ("a date range",
         "hledger register expenses -b 2026-04-01 -e 2026-06-01"),
        ("this month / last month",
         "hledger register expenses -p 'this month'   # or 'last month'"),
        ("year to date",
         "hledger balance expenses -b 2026-01-01"),
    ]),
    ("statements & summaries", [
        ("income statement for one month",
         "hledger incomestatement -p 2026-05"),
        ("income statement, month by month",
         "hledger incomestatement -M"),
        ("balance sheet (what you own & owe)",
         "hledger balancesheet"),
        ("spending by category, monthly columns",
         "hledger balance expenses -M"),
        ("monthly totals for one category",
         "hledger balance expenses:groceries -M"),
        ("top-level summary only (collapse subaccounts)",
         "hledger balance expenses --depth 2"),
    ]),
    ("the budget", [
        ("envelopes vs actuals, monthly",
         "hledger balance --budget -M expenses"),
        ("just the current month",
         "hledger balance --budget -p 'this month' expenses"),
    ]),
    ("budge-specific", [
        ("only unreviewed (pending !) transactions",
         "hledger register --pending"),
        ("only reviewed/cleared transactions",
         "hledger register --cleared expenses:dining"),
        ("is the transfers clearing account balanced?",
         "hledger register assets:transfers   # should trend to zero"),
        ("find a transaction by its bank id",
         "hledger print tag:simplefin_id=TXN123"),
        ("when did budge last reconcile each account?",
         "hledger print desc:'balance assertion'"),
    ]),
    ("exporting & misc", [
        ("any report as CSV (for a spreadsheet)",
         "hledger balance expenses -M -O csv > spend.csv"),
        ("account names budge knows about",
         "hledger accounts"),
        ("payees seen in the journal",
         "hledger payees"),
        ("sanity-check the whole journal",
         "hledger check"),
        ("count/size stats for the journal",
         "hledger stats"),
    ]),
]

TIPS = """\
tips:
  - quote query args with special chars: 'amt:>100' (the shell eats > and $)
  - queries are case-insensitive regexes: desc:kroger matches KROGER #123
  - combine any filters; space means AND: expenses:dining date:2026/05
  - register = running list; balance = totals; print = full entries
  - full manual: https://hledger.org/hledger.html (or: man hledger)
"""


def run_cheatsheet(topic: str = "") -> None:
    topic = (topic or "").lower()
    shown = 0
    for title, entries in SECTIONS:
        matching = [
            (what, cmd) for what, cmd in entries
            if not topic or topic in title.lower()
            or topic in what.lower() or topic in cmd.lower()
        ]
        if not matching:
            continue
        header(title)
        for what, cmd in matching:
            say(f"  {what}")
            base, _, comment = cmd.partition("#")
            say("    " + paint(base.rstrip(), "cyan", "bold")
                + (paint(" #" + comment, "dim") if comment else ""))
            shown += 1
    if shown == 0:
        say(f"nothing matching {topic!r} — try `budge cheatsheet` for "
            "everything, or a broader word like 'month' or 'vendor'")
        return
    say("")
    note(TIPS)
