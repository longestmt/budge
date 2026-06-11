"""Test fixtures: temp config + data repo, fake SimpleFIN server, fake AI.

The fake SimpleFIN server speaks the real protocol shape (claim exchange,
/accounts with start-date); the fake AI is the `command` provider — a script
that answers from a JSON map, so tests are deterministic and offline.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from budge import util  # noqa: E402
from budge.config import Config  # noqa: E402
from budge.scaffold import (declare_account, save_accounts, scaffold,  # noqa: E402
                            seed_account_rules)
from budge.gitutil import git  # noqa: E402

HLEDGER = os.environ.get("BUDGE_TEST_HLEDGER",
                         str(Path.home() / ".local" / "bin" / "hledger"))


# ------------------------------------------------------ fake SimpleFIN -----

class FakeSimpleFIN:
    """In-process SimpleFIN Bridge stand-in."""

    def __init__(self):
        self.accounts = []          # list of account dicts (protocol shape)
        self.messages = []          # entries for the protocol `errors` array
        self.claimed_tokens = set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if self.path.startswith("/claim/"):
                    token_id = self.path.split("/")[-1]
                    if token_id in outer.claimed_tokens:
                        self.send_response(403)
                        self.end_headers()
                        return
                    outer.claimed_tokens.add(token_id)
                    body = outer.access_url.encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/accounts":
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                start = int(qs.get("start-date", ["0"])[0])
                accounts = []
                for acct in outer.accounts:
                    a = dict(acct)
                    a["transactions"] = [
                        t for t in acct.get("transactions", [])
                        if int(t.get("posted", 0)) >= start
                    ]
                    accounts.append(a)
                body = json.dumps(
                    {"errors": list(outer.messages),
                     "accounts": accounts}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.access_url = f"http://user:pass@127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def setup_token(self, token_id: str = "tok1") -> str:
        return base64.b64encode(
            f"{self.base}/claim/{token_id}".encode()).decode()

    def stop(self):
        self.server.shutdown()


@pytest.fixture()
def simplefin_server():
    server = FakeSimpleFIN()
    os.environ["BUDGE_FAKE_SIMPLEFIN"] = server.access_url
    yield server
    os.environ.pop("BUDGE_FAKE_SIMPLEFIN", None)
    server.stop()


# ------------------------------------------------------------- fake AI -----

FAKE_AI_SCRIPT = '''#!/usr/bin/env python3
"""Fake AI provider (budge `command` provider). Reads the prompt on stdin;
the user payload is the final line (compact JSON). Behavior comes from the
JSON file at $BUDGE_FAKE_AI_MAP:
  {"malformed": true}                      -> reply with non-JSON garbage
  {"map": {PAYEE: {...decision...}}, "default": {...} | null,
   "plan": {...wizard reply...}}
"""
import json, os, sys

spec = json.load(open(os.environ["BUDGE_FAKE_AI_MAP"]))
text = sys.stdin.read()
if spec.get("malformed"):
    print("hmm, these look like groceries to me, mostly! no json today.")
    sys.exit(0)
payload = json.loads(text.strip().splitlines()[-1])
if "merchants" in payload:
    print(json.dumps(spec.get("plan", {})))
    sys.exit(0)
out = []
for txn in payload.get("transactions", []):
    d = spec.get("map", {}).get(txn["payee"], spec.get("default"))
    if d is None:
        continue
    d = dict(d)
    d["id"] = txn["id"]
    d.setdefault("confidence", "medium")
    d.setdefault("rationale", "fake ai decision")
    d.setdefault("transfer", False)
    out.append(d)
print(json.dumps({"transactions": out}))
'''


@pytest.fixture()
def fake_ai(tmp_path):
    script = tmp_path / "fake_ai.py"
    script.write_text(FAKE_AI_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    map_file = tmp_path / "ai_map.json"
    map_file.write_text("{}", encoding="utf-8")
    os.environ["BUDGE_FAKE_AI_MAP"] = str(map_file)

    class FakeAI:
        path = str(script)

        @staticmethod
        def respond(spec: dict):
            map_file.write_text(json.dumps(spec), encoding="utf-8")

    yield FakeAI
    os.environ.pop("BUDGE_FAKE_AI_MAP", None)


# ----------------------------------------------------------- environment ---

@pytest.fixture()
def env(tmp_path, fake_ai, monkeypatch):
    """Temp HOME, config dir, scaffolded data repo with two mapped accounts."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")

    confdir = tmp_path / "config"
    confdir.mkdir()
    monkeypatch.setenv("BUDGE_CONFIG_DIR", str(confdir))
    monkeypatch.setenv("BUDGE_HLEDGER", HLEDGER)
    util.DRY_RUN = False

    repo = tmp_path / "data"
    monkeypatch.setenv("BUDGE_REPO", str(repo))

    (confdir / "budge.conf").write_text(
        "[repo]\n"
        f"path = {repo}\n"
        "[ai]\n"
        "provider = command\n"
        f"model = {fake_ai.path}\n"
        "[notify]\n"
        "openclaw_url =\n",
        encoding="utf-8",
    )
    (confdir / "secrets.env").write_text(
        "SIMPLEFIN_ACCESS_URL=http://user:pass@placeholder.invalid\n",
        encoding="utf-8",
    )

    scaffold(repo)
    accounts = [
        {"id": "act-checking", "name": "TestBank Checking",
         "slug": "checking", "account": "assets:checking", "currency": "$"},
        {"id": "act-card", "name": "TestBank Card",
         "slug": "card", "account": "liabilities:card", "currency": "$"},
        {"id": "act-invest", "name": "TestBank Stock Plan",
         "slug": "stock-plan", "account": "assets:stock-plan",
         "currency": "$", "drift": True},
    ]
    save_accounts(repo, accounts)
    for a in accounts:
        declare_account(repo, a["account"])
        seed_account_rules(repo, a)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "test scaffold", "-q")

    class Env:
        pass

    e = Env()
    e.repo = repo
    e.confdir = confdir
    e.cfg = Config()
    e.fake_ai = fake_ai
    return e


# --------------------------------------------------------------- helpers ---

def txn(tid, posted_date, amount, payee, memo=""):
    import datetime as dt
    import time as _time
    d = dt.date.fromisoformat(posted_date)
    posted = int(_time.mktime(d.timetuple())) + 12 * 3600
    return {"id": tid, "posted": posted, "amount": str(amount),
            "payee": payee, "description": payee, "memo": memo}


def checking_account(transactions, balance):
    return {"id": "act-checking", "name": "Checking",
            "org": {"name": "TestBank"}, "currency": "USD",
            "balance": str(balance), "transactions": transactions}


def card_account(transactions, balance):
    return {"id": "act-card", "name": "Card",
            "org": {"name": "TestBank"}, "currency": "USD",
            "balance": str(balance), "transactions": transactions}


def invest_account(balance, transactions=()):
    return {"id": "act-invest", "name": "Stock Plan",
            "org": {"name": "TestBank"}, "currency": "USD",
            "balance": str(balance), "transactions": list(transactions)}


def consistent_balance(opening, transactions):
    """balance such that opening + sum(txns) == balance (assertions pass)."""
    return round(opening + sum(float(t["amount"]) for t in transactions), 2)


@pytest.fixture()
def answers(monkeypatch):
    """Scripted input() answers for interactive flows."""
    queue = []

    def fake_input(prompt_text=""):
        if not queue:
            raise EOFError
        return str(queue.pop(0))

    monkeypatch.setattr("builtins.input", fake_input)
    return queue
