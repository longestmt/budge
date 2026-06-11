<p align="center">
  <img src="assets/logo.png" alt="budge — a thumbs-up budgie with a terminal and a coin" width="420">
</p>

**budge is a self-hosted household budgeting system that runs itself.** Bank
transactions flow in automatically every morning, an AI takes a first pass at
categorizing them, you spend ten minutes a week confirming its work, and your
family checks a simple dashboard to answer the only question that matters:
*"how much is left in each category?"*

## Why does this exist?

Budgeting apps make you choose between two bad options:

- **Hosted services** (Mint, YNAB, Monarch...) own your data, charge rent,
  change their pricing, get acquired, and shut down. Your financial history
  lives in someone else's database.
- **Manual plain-text accounting** (hledger, ledger, beancount) gives you full
  ownership but demands constant typing and categorizing. Most people burn out
  within months.

budge takes a third path: **your data stays in plain text files you own
forever, while automation does the tedious parts.** The result feels like a
hosted app day to day, but every transaction lives in a human-readable file in
a git repository on your own hardware.

A few principles fall out of that:

- **Plain text is the database.** Your books are ordinary files readable in
  any editor, versioned by git. No lock-in — every tool can be replaced
  without touching the data.
- **The AI suggests; you decide.** Nothing the AI categorizes becomes
  permanent until you approve it. Every AI decision is logged.
- **The budget never lies optimistically.** Even unreviewed transactions
  count against your budget immediately, so the dashboard never shows more
  money than you actually have.
- **The system gets smarter weekly.** Every correction you make becomes a
  permanent rule, so the AI's share of the work shrinks over time.

## What budge is made of

budge is the glue around four boring, proven, completely-stock tools:

| Tool | Role |
|---|---|
| [hledger](https://hledger.org) | the accounting engine and system of record |
| [SimpleFIN Bridge](https://beta-bridge.simplefin.org) | secure read-only feed of your bank transactions |
| [Paisa](https://paisa.fyi) | the web dashboard your household actually looks at |
| git + GitHub | history, backup, and sync |

budge itself is a small Python CLI (no dependencies) that orchestrates the
workflows those tools don't have: fetching, AI categorization, weekly review,
and budget planning. It deliberately does **not** wrap their features — when
you want a report, you ask hledger directly, and the skills you learn are
hledger skills, not budge skills.

## How a transaction flows through the system

```
Banks → SimpleFIN Bridge → budge fetch (immutable raw CSVs, committed)
            │
            ▼
   hledger CSV rules ──match──▶ main.journal (cleared *)   [never reviewed]
            │ no match
            ▼
   budge categorize (AI) ──▶ pending.journal (status !) + ai-decisions.log
            │                       ▲ regenerated from raw CSVs
            ▼                       │
   weekly  budge review ──corrections──▶ rules files
            │
        promote: hledger check ⇒ flip ! to * ⇒ append to main.journal
                 ⇒ clear pending ⇒ one commit ⇒ push
            │
            ▼
   Paisa (reads the journal incl. pending) → household dashboard
```

In words: every morning budge pulls new transactions. Ones that match a
deterministic rule ("KROGER → groceries") go straight into the books as
trusted. The rest get an AI best guess and sit in a **pending** state —
visible to the budget immediately, but marked unconfirmed. Once a week you
review pending items *grouped by vendor* (one decision covers many
transactions), and each correction becomes a rule so that vendor never needs
review again. A category is **decided at import but only trusted after your
review.**

## What daily life looks like

| When | What happens | Who does it |
|---|---|---|
| every morning | new transactions appear in the books, zero manual steps, zero duplicates; a balance assertion cross-checks the books against the bank's reported balance | automatic |
| weekly | you get a nudge ("review ready — 14 pending"), run `budge review`, and confirm/correct vendor by vendor — under ten minutes | you |
| quarterly-ish | `budge plan` compares envelopes to reality ("dining 32% over for 3 months") and proposes adjustments you approve or decline | you |
| anytime | the Paisa dashboard shows what's left in each envelope | anyone in the house |

Reporting is plain hledger whenever you want it:

```sh
hledger -f main.journal balance --budget -M expenses
hledger -f main.journal register assets:transfers
```

## The budget wizard

`budge plan` sets up (and later re-tunes) your budget from your *actual*
spending, not a blank form. It asks exactly three things — monthly take-home,
monthly savings target, and "what are you saving for / what's changing this
year?" — then analyzes your imported history and proposes three artifacts you
confirm or edit: a chart of categories, monthly envelope amounts (income −
savings = ceiling; the math conflicts are surfaced, never auto-resolved), and
20–30 starter vendor rules. It cites only your own numbers — no "families
like yours typically spend..." It's a bookkeeping assistant, not a financial
advisor.

## Privacy and safety properties

- **Two separate repos.** This repo is *code only* and contains no financial
  data. Your books live in a second repo that setup creates — keep that one
  **private**. (See the layout below.)
- **Secrets never enter either repo**: bank access URL and AI key live in
  `~/.config/budge/secrets.env`, chmod 600.
- **The AI sees the minimum**: payee, amount, date, and which account — never
  account numbers, balances, or your full history. Enforced in code
  regardless of provider; switching to a fully local model is a one-line
  config change.
- **Append-only audit trail**: every AI suggestion, rejection, and review
  outcome is logged in `ai/ai-decisions.log`.
- **Hard gate on the books**: nothing is promoted, committed, or pushed
  unless `hledger check` passes.
- **The notifier (OpenClaw) is outbound-only** — there is no code path by
  which it, or the AI, can write to your journal, rules, or git.

## Getting started (Debian 13)

```sh
sudo ./setup.sh
```

That installs the prerequisites and walks you through everything
interactively: connecting SimpleFIN (a one-time token exchange; have your
setup token ready), naming your accounts, a 90-day history backfill with
computed opening balances, scheduling the daily timers, pointing Paisa at the
books, and finally the budget wizard. It's safe to re-run at any point.

You'll want ready: a SimpleFIN Bridge setup token, an AI provider + key
(Ollama cloud, OpenAI, or Anthropic), a **private** GitHub repo URL for the
books, and (optionally) your OpenClaw notification endpoint.

## The data repo (created by setup, separate from this one)

```
budge/                      # your books — private git repo
  main.journal              # SOURCE OF TRUTH; includes the files below
  pending.journal           # DERIVED: AI-categorized, awaiting review (!)
  budget.journal            # monthly envelopes (written by the wizard)
  accounts.journal          # chart of accounts
  household.md              # income, savings target, goals, decision log
  import/
    rules/<account>.rules   # hledger CSV rules; grow with every correction
    raw/YYYY-MM/*.csv       # immutable record of what the bank sent
    state/<account>.ids     # duplicate-prevention state
  ai/
    agent.md                # the categorizer's instructions (editable!)
    ai-decisions.log        # append-only audit log
  systemd/                  # rendered service/timer files
  paisa.yaml                # dashboard config
```

## If something dies (the replaceability map)

| Component dies | What you do |
|---|---|
| your bank drops SimpleFIN | export CSV manually into `import/raw/` — identical import path |
| AI provider outage | transactions queue as uncategorized; books stay correct in total; it catches up next run |
| budge itself | the data is plain hledger + CSV + git; every script is replaceable from its module docstring |
| Paisa | any hledger-compatible dashboard reads the same journal |

## For the curious: notable design decisions

- Duplicate prevention is by bank transaction ID, stronger than hledger's
  date-based `.latest` mechanism; re-runs and re-backfills can never import
  a transaction twice.
- Card payments and transfers post through a clearing account
  (`assets:transfers`) from both feeds and net to zero — never counted as
  spending. Transfer patterns sit last in each rules file so they outrank
  vendor rules.
- budge maintains one *current* balance assertion per account, superseding
  its own previous mark each fetch (a pinned historical assertion breaks
  legitimately when a late-posting transaction arrives); git history keeps
  every superseded one.
- The decision log is event-sourced: review outcomes are appended as new
  events, never rewritten over history.
- A vendor correction moves all matching pending transactions straight into
  the books as cleared — they're now deterministic rule matches, the same
  path import takes — and that vendor never re-enters review.
- One-off (non-rule) corrections are logged as `manual_override` events so
  they survive regeneration of the derived pending file.
- Every command supports `--dry-run`: prints intended actions, writes
  nothing.

## Tests

```sh
python3 -m pytest tests/        # needs hledger >= 1.25 on PATH
                                # (or BUDGE_TEST_HLEDGER=/path/to/hledger)
```

32 tests cover the PRD acceptance criteria that are exercisable off-box
(A2–A9, A11–A15), using a fake SimpleFIN server that speaks the real protocol
and a deterministic fake AI provider. A1/A10 (fresh-LXC setup and systemd
failure alerts) need a real Debian box.
