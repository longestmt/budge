"""Thin shell-outs to the stock hledger binary.

This module is the ONLY place budge invokes hledger. Everything goes through
the public CLI — never internals — per the prime directive. The operator-facing
accounting interface remains hledger itself; these calls are plumbing for
workflows hledger does not have (import splitting, promote gating).
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .util import run


def _bin() -> str:
    return Config().hledger_bin()


def hledger(args, cwd=None, check=True):
    return run([_bin()] + [str(a) for a in args], cwd=cwd, check=check)


def check(journal: Path, extra_checks=None) -> tuple:
    """Run `hledger check` (the hard gate). Returns (ok, output)."""
    args = ["check", "-f", str(journal)]
    if extra_checks:
        args += list(extra_checks)
    proc = hledger(args, check=False)
    ok = proc.returncode == 0
    return ok, (proc.stdout + proc.stderr).strip()


def csv_to_entries(csv_path: Path, rules_path: Path) -> list:
    """Convert a CSV through an hledger rules file into journal entry texts.

    Uses `hledger -f csv:FILE --rules-file RULES print` — the stable, documented
    interface for CSV conversion. Returns a list of entry strings.
    """
    proc = hledger(
        ["-f", f"csv:{csv_path}", "--rules-file", str(rules_path), "print"]
    )
    return split_entries(proc.stdout)


def split_entries(journal_text: str) -> list:
    """Split `hledger print` output into individual entry strings."""
    entries, current = [], []
    for line in journal_text.splitlines():
        if re.match(r"^\d{4}[-/.]\d{2}[-/.]\d{2}", line):
            if current:
                entries.append("\n".join(current).rstrip())
            current = [line]
        elif line.strip() and current:
            current.append(line)
        elif not line.strip() and current:
            entries.append("\n".join(current).rstrip())
            current = []
    if current:
        entries.append("\n".join(current).rstrip())
    return [e for e in entries if e.strip()]


def balance(journal: Path, query, extra=None) -> str:
    args = ["balance", "-f", str(journal)] + list(query)
    if extra:
        args += list(extra)
    return hledger(args).stdout


def account_balance(journal: Path, account: str) -> str:
    """Single-account total via `hledger balance ACCT` (empty string if zero)."""
    proc = hledger(
        ["balance", "-f", str(journal), account, "--format", "%(total)"],
        check=False,
    )
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def declared_accounts(journal: Path) -> list:
    """Accounts declared (and used) in the journal, via `hledger accounts`."""
    proc = hledger(["accounts", "-f", str(journal), "--declared"], check=False)
    if proc.returncode != 0:  # older hledger: fall back to all accounts
        proc = hledger(["accounts", "-f", str(journal)], check=False)
    return [l.strip() for l in proc.stdout.splitlines() if l.strip()]
