"""Conversation and guarded budget mutation tests for ``budge talk``."""

import datetime as dt
import json

from budge import util
from budge.gitutil import git
from budge.plan import read_budget
from budge.talk import (TALK_SYSTEM, TalkSession, TalkTUI, _parse_reply,
                        run_talk)


def _seed_budget(env):
    (env.repo / "accounts.journal").write_text(
        (env.repo / "accounts.journal").read_text(encoding="utf-8")
        + "account expenses:dining\n"
        + "account expenses:groceries\n",
        encoding="utf-8",
    )
    (env.repo / "budget.journal").write_text(
        "~ monthly\n"
        "    expenses:dining       $400.00\n"
        "    expenses:groceries    $650.00\n"
        "    assets:checking\n",
        encoding="utf-8",
    )
    (env.repo / "household.md").write_text(
        "# household.md\n\n"
        "- Monthly take-home income: $3000.00\n"
        "- Monthly savings target: $500.00\n\n"
        "## Goals & upcoming changes\n\nBuild an emergency fund.\n\n"
        "## Decision log\n",
        encoding="utf-8",
    )


def test_talk_prompt_limits_when_agent_changes_budget():
    assert "an expert on\nBudge and hledger" in TALK_SYSTEM
    assert "`budge balance` or `budge report`" in TALK_SYSTEM
    assert "hledger -f main.journal balance --budget" in TALK_SYSTEM
    assert "pending (`!`)" in TALK_SYSTEM
    assert "authorized to change" in TALK_SYSTEM
    assert "clearly asks" in TALK_SYSTEM
    assert "only set an\nexisting category" in TALK_SYSTEM
    assert "never\nclaim that a change succeeded" in TALK_SYSTEM


def test_talk_rejects_unknown_and_invalid_budget_actions():
    reply = _parse_reply(json.dumps({
        "message": "I can make that adjustment.",
        "budget_changes": [
            {"category": "expenses:travel", "monthly_amount": 100},
            {"category": "expenses:dining", "monthly_amount": -1},
            {"category": "expenses:dining", "monthly_amount": True},
        ],
    }), {"expenses:dining"})

    assert reply.message == "I can make that adjustment."
    assert reply.requested == []
    assert len(reply.notices) == 3


def test_talk_session_applies_checks_logs_and_commits(
        env, monkeypatch):
    _seed_budget(env)
    with open(env.repo / "main.journal", "a", encoding="utf-8") as journal:
        journal.write(
            f"\n{dt.date.today().isoformat()} * CAFE LOCAL  ; "
            "simplefin_id:secret-123\n"
            "    expenses:dining        $25.00\n"
            "    assets:checking       $-25.00\n")
    prompts = []

    def fake_complete(cfg, system, user):
        prompts.append((system, json.loads(user)))
        return json.dumps({
            "message": "Dining can be set to $250 per month.",
            "budget_changes": [{
                "category": "expenses:dining",
                "monthly_amount": 250,
                "reason": "requested by the user",
            }],
        })

    monkeypatch.setattr("budge.talk.ai.complete", fake_complete)
    reply = TalkSession(env.cfg).ask("Set dining to $250.")

    assert [(c.category, c.monthly_amount) for c in reply.applied] == [
        ("expenses:dining", 250.0)]
    assert read_budget(env.repo)["expenses:dining"] == 250.0
    assert "expenses:dining: $400.00 -> $250.00" in (
        env.repo / "household.md").read_text(encoding="utf-8")
    assert "budge talk: budget update" in git(
        env.repo, "log", "-1", "--pretty=%s").stdout
    payload = prompts[0][1]
    assert payload["user_message"] == "Set dining to $250."
    assert payload["household_context"]["current_monthly_budget"] == {
        "expenses:dining": 400.0, "expenses:groceries": 650.0}
    month_to_date = payload["household_context"]["current_month_to_date"]
    assert month_to_date["partial_through"]
    assert month_to_date["spend_to_date"] == 25.0
    assert month_to_date["categories"][0]["top_merchants"] == [{
        "payee": "CAFE LOCAL", "spent_to_date": 25.0}]
    serialized = json.dumps(payload)
    assert "act-checking" not in serialized
    assert "TestBank" not in serialized
    assert "secret-123" not in serialized


def test_talk_rolls_back_when_hledger_rejects_change(env, monkeypatch):
    _seed_budget(env)
    before = (env.repo / "budget.journal").read_text(encoding="utf-8")
    monkeypatch.setattr("budge.talk.ai.complete", lambda *args: json.dumps({
        "message": "I'll request that change.",
        "budget_changes": [{
            "category": "expenses:dining", "monthly_amount": 200,
        }],
    }))
    monkeypatch.setattr("budge.talk.hledger.check",
                        lambda *args: (False, "test rejection"))

    reply = TalkSession(env.cfg).ask("Set dining to $200.")

    assert reply.applied == []
    assert any("rolled back" in notice for notice in reply.notices)
    assert (env.repo / "budget.journal").read_text(
        encoding="utf-8") == before
    assert "APPLIED" not in (env.repo / "household.md").read_text(
        encoding="utf-8")


def test_talk_dry_run_does_not_open_tui_or_call_ai(
        env, monkeypatch, capsys):
    _seed_budget(env)
    monkeypatch.setattr("budge.talk.ai.complete", lambda *args: (_ for _ in ())
                        .throw(AssertionError("AI should not be called")))
    util.DRY_RUN = True
    try:
        run_talk(env.cfg)
    finally:
        util.DRY_RUN = False
    assert "no AI call or write" in capsys.readouterr().out


def test_talk_tui_renders_and_budget_command_is_local(env):
    _seed_budget(env)

    class Screen:
        def erase(self):
            pass

        def getmaxyx(self):
            return 24, 80

        def addstr(self, *args):
            pass

        def move(self, *args):
            pass

        def refresh(self):
            pass

    tui = TalkTUI(Screen(), TalkSession(env.cfg))
    tui._draw()

    assert tui._command("/budget") is False
    assert tui.transcript[-1] == (
        "Budget", "expenses:dining: $400.00/month\n"
        "expenses:groceries: $650.00/month")
