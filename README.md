# budge

Glue scripts, repository scaffolding, and automation around a plain-text
accounting stack: **hledger** (system of record), **SimpleFIN Bridge** (bank
data), **Paisa** (dashboard), and **git** (history/sync). Implements
`budge-PRD-v1.1.pdf`.

**Prime directive — no frankensteining.** hledger, Paisa, and SimpleFIN Bridge
are completely stock. Everything in this repo lives at the seams and speaks
only stable interfaces: the SimpleFIN API, CSV, the hledger journal format,
and the hledger CLI. Every script is individually replaceable without touching
the data. There is deliberately no `budge balance` and no `budge report` —
reporting and queries are plain hledger, which is the accounting interface you
learn and keep.

## How it works

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

A transaction's category is **decided at import but only trusted at promote**.
Pending entries are included in every report (via `include pending.journal`
and the `!` status), so envelopes are drawn down immediately at the AI's best
guess — the dashboard never overstates what's left. Review operates on
**vendors**, not transactions: a correction writes a rule and regenerates
pending from the raw CSVs. Raw CSVs and main.journal are the records;
pending.journal is derived and disposable.

## Install (Debian 13)

```sh
sudo ./setup.sh        # installs hledger, git, podman, the budge CLI,
                       # then drops to your user for interactive `budge setup`
```

`budge setup` is idempotent and walks through: SimpleFIN claim exchange (the
one-time setup token is exchanged for a permanent access URL and never
persisted), account mapping, repo scaffold with seeded transfer/payment rules,
the 90-day backfill with computed opening balances and balance assertions
(with an explained-discrepancy flow if a bank-pending transaction skews the
math), systemd timer install, Paisa config, and finally the budget wizard.

Secrets live in `~/.config/budge/secrets.env` (chmod 600), **outside** the
data repo. Machine-local settings live in `~/.config/budge/budge.conf`.

## Daily life

| When | What | Who |
|---|---|---|
| every morning | `budge-fetch` pulls transactions; rules import what they can as cleared; a balance assertion reconciles against the bank | timer |
| right after | `budge-categorize` suggests categories for the rest into pending | timer |
| after both | `budge-push` syncs the repo to GitHub | timer |
| weekly | OpenClaw nudges "review ready — N pending"; you run `budge review` (vendor-grouped, ~10 min; every correction becomes a rule) | you |
| quarterly-ish | `budge plan` re-assesses envelopes against actuals | you |
| anytime | the Paisa dashboard answers "how much is left in each category?" | spouse |

Reporting is stock hledger:

```sh
hledger -f main.journal balance --budget -M expenses
hledger -f main.journal register assets:transfers
hledger -f main.journal check
```

Every budge command accepts `--dry-run` (prints intended actions, writes
nothing).

## Data repo layout (scaffolded by setup, PRD §5)

```
budge/                      # git repo, synced to GitHub
  main.journal              # SOURCE OF TRUTH; includes the files below
  pending.journal           # DERIVED: AI-categorized, status !
  budget.journal            # ~ monthly envelopes (written by the wizard)
  accounts.journal          # chart of accounts declarations
  household.md              # income, savings target, goals, decision log
  import/
    rules/<account>.rules   # stock hledger CSV rules, one per account
    raw/YYYY-MM/*.csv       # immutable SimpleFIN output, committed
    state/<account>.ids     # transaction-ID dedup state
  ai/
    agent.md                # categorizer instructions (operator-editable)
    ai-decisions.log        # append-only JSONL audit log
  systemd/                  # units rendered with concrete paths by setup
  paisa.yaml                # stock Paisa config pointed at main.journal
```

## Design decisions worth knowing

- **Dedup** is by SimpleFIN transaction ID (`import/state/<account>.ids`,
  plus a scan of `simplefin_id:` tags already in the journals). This is
  strictly stronger than hledger's date-based `.latest` files; re-runs and
  re-backfills can never import a transaction twice.
- **Transfers** post against the clearing account `assets:transfers` from
  both feeds and net to zero. Transfer patterns sit *last* in each rules file
  so they outrank vendor rules; `budge review` warns when the clearing
  account doesn't net to zero. (hledger CSV `if` patterns are
  case-insensitive POSIX regexps; budge writes `%payee`-scoped matchers.)
- **Balance assertions**: budge maintains exactly one current assertion per
  account, superseding its own previous mark each fetch. A pinned historical
  assertion breaks legitimately when a late-posting transaction arrives in a
  later pull; git history preserves every superseded assertion.
- **The decision log is event-sourced.** The PRD asks for an append-only log
  whose accepted/overridden field is "updated" at promote; budge resolves
  that by appending `outcome` events instead of rewriting history.
- **Vendor corrections move matching transactions out of pending** into
  main.journal as cleared, because they are now deterministic rule matches —
  the same path import takes (PRD §4: rule-matched is never reviewed). The
  corrected vendor never re-enters pending.
- **One-off corrections** are logged as `manual_override` events, so
  regeneration (pending is derived!) preserves them.
- **AI write surface** is pending.journal + the log, nothing else — no
  commits, no rules edits, no main.journal. Malformed model output is
  rejected; the transaction stays `expenses:uncategorized !` rather than
  guessed at. The request payload is payee/description, amount, date, source
  account name only — enforced in `budge/ai.py:minimized_payload` regardless
  of provider.
- **Providers**: `openai-compatible` (Ollama cloud/local, OpenAI — switching
  to a local model is a base_url change), `anthropic`, and `command` (a local
  executable; used by the test suite, handy offline).
- **OpenClaw is outbound-only.** The notifier sends unit name, exit status,
  and a journalctl tail; there is no code path by which OpenClaw writes to
  the journal, rules, secrets, or git.

## Replaceability map (the point of the seams)

| Component dies | What you do |
|---|---|
| SimpleFIN | export CSV from the bank into `import/raw/`, same rules path |
| AI provider | change `ai.base_url`/`ai.model`; or run uncategorized — books stay correct in total |
| budge itself | the data is plain hledger + CSV + git; any script here can be rewritten from its module docstring |
| Paisa | any hledger-compatible dashboard reads main.journal |

## Tests

```sh
python3 -m pytest tests/        # needs hledger >= 1.25 on PATH
                                # (or BUDGE_TEST_HLEDGER=/path/to/hledger)
```

32 tests cover the PRD acceptance criteria that are exercisable off-box:
A2–A9, A11–A15 (A1/A10 need a real Debian LXC with systemd — see
`systemd/*.in` and the OnFailure wiring). The suite runs a fake SimpleFIN
server speaking the real protocol (including claim/already-claimed) and a
deterministic fake AI via the `command` provider.
