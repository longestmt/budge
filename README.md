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
budget planning, cashflow-aware consultation, and an interactive budget
conversation. It deliberately does **not** wrap their features — when you want
a report, you ask hledger directly, and the skills you learn are hledger
skills, not budge skills.

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
        promote: hledger check ⇒ flip ! to * ⇒ insert at main transaction marker
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
| quarterly-ish | `budge consult` reviews completed-month cashflow and spending, proposes new envelopes, and shows practical cuts with monthly and annual savings | you |
| anytime | `budge talk` opens a terminal conversation about the household budget and can make requested envelope changes | you + AI |
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

## Budget consultation

`budge consult` is the deeper periodic checkup after a budget exists. It uses
the available completed calendar months (six by default), so a half-finished
current month cannot distort the result. The consultation shows:

- observed monthly income, spending, the household savings target, and the
  resulting spending ceiling;
- every current envelope beside its adjusted completed-month average;
- a proposed amount for every envelope, including increases where the current
  number is no longer realistic; and
- category- and merchant-grounded ideas for reducing spend, with each
  potential reduction shown per month and per year.

The configured AI receives only those aggregates and the top merchant totals
inside each category—not full transactions, account numbers, or balances.
Budge validates the returned categories and amounts and calculates all savings
itself. The proposal is displayed as a `budget.journal` diff and is written
only after explicit approval and a passing `hledger check`.

```sh
budge consult                 # up to six completed months
budge consult --months 12     # longer view for seasonal spending
```

## Budge Talk

`budge talk` opens a full-screen terminal conversation with the configured AI.
It can explain completed-month spending patterns and clearly labeled
month-to-date totals, compare them with the current envelopes, explore
tradeoffs, explain Budge's transaction and review workflows, teach hledger
commands and accounting concepts, and make budget changes you clearly request
or accept during the conversation. Its responsive retrowave interface uses the
Budge navy, neon-green, cyan, and purple palette; wide terminals add a live
month-to-date budget sidebar with envelope progress and overspend warnings.

```sh
budge talk                    # six completed months of context
budge talk --months 12        # use a longer aggregate history
```

Talk sends the same minimized data used by consultation, plus current
month-to-date aggregates: category and top-merchant totals, current envelopes,
and household-stated goals. It never sends account numbers, balances, or the
full journal. The agent can set existing envelope amounts, but cannot create
accounts or edit transactions. Each requested change is validated locally,
rolled back if `hledger check` fails, appended to the household decision log,
and committed. Use `/budget` to show the live envelopes, `/clear` to forget the
current conversation, PgUp/PgDn to scroll, and `/quit` to exit.

## Privacy and safety properties

- **Two separate repos.** This repo is *code only* and contains no financial
  data. Your books live in a second repo that setup creates — keep that one
  **private**. (See the layout below.)
- **Secrets never enter either repo**: bank access URL and AI key live in
  `~/.config/budge/secrets.env`, chmod 600.
- **The AI sees the minimum for each job**: categorization gets payee, amount,
  date, and source account; planning, consultation, and Talk get
  monthly/category aggregates, stated goals, and top merchant totals. It never
  receives account numbers, balances, or the full journal. Switching to a fully
  local model is a one-line config change.
- **Append-only audit trail**: every AI suggestion, rejection, and review
  outcome is logged in `ai/ai-decisions.log`.
- **Hard gate on the books**: nothing is promoted, committed, or pushed
  unless `hledger check` passes.
- **The notifier (OpenClaw) is outbound-only.** Budge Talk gives agent-requested
  actions one narrow write path: it can set existing budget envelopes through
  local validation, the hledger check gate, an audit entry, and a git commit.
  It cannot edit transactions, categorization rules, or account declarations.

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

## Updating Budge

The installer puts an isolated snapshot of Budge in `pipx`; pulling this code
repository does not silently change the command used by timers. Check for a
new stable release, then install it explicitly:

```sh
budge update --check
budge update
```

Updates come only from stable `vX.Y.Z` tags in the official Budge repository.
The tag is resolved to its exact commit before installation, the replacement
CLI must start and report the expected version, and Budge attempts to restore
the previous tagged release if verification fails. The updater refreshes
generated systemd unit files but never changes journals, transaction data,
the private books repository, Budge settings, secrets, system packages, or UI
dependencies. Use
`budge update --no-services` to update only the CLI. If system directories are
not writable, Budge prints the exact administrator commands still needed.

Automatic installation is deliberately not scheduled. `budge update --check`
is read-only and suitable for a periodic availability notification.

To publish an update, bump `budge.__version__`, commit it, create the matching
annotated tag (for example `v1.1.0`), and push the tag. The updater rejects
pre-release and non-semantic tags, and verification fails if the version in
the tagged package does not match the tag.

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

The test suite covers the PRD acceptance criteria that are exercisable off-box
(A2–A9, A11–A15), plus the consultation workflow, using a fake SimpleFIN
server that speaks the real protocol and a deterministic fake AI provider.
A1/A10 (fresh-LXC setup and systemd failure alerts) need a real Debian box.
