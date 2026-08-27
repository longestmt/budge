"""Conversation and guarded budget mutation tests for ``budge talk``."""

import datetime as dt
import json

from budge import util
from budge.gitutil import git
from budge.plan import read_budget
from budge.talk import (TALK_SYSTEM, TalkSession, TalkTUI, _clean_message,
                        _parse_reply, run_talk)


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


def test_talk_recovers_prose_from_malformed_fenced_json():
    # Models sometimes put valid message JSON inside an object with a missing
    # comma. Talk should show the prose, not its transport envelope, while
    # continuing to reject all actions from that malformed object.
    raw = r'''```json
{
  "message": "The largest merchant total is **CAFE LOCAL at $169.64**, but that may cover several visits.\n\nRun:\n\n```\nhledger -f main.journal register expenses:dining -M\n```"
  "budget_changes": []
}
```'''

    reply = _parse_reply(raw, {"expenses:dining"})

    assert reply.message == (
        "The largest merchant total is CAFE LOCAL at $169.64, but that may "
        "cover several visits.\n\nRun:\n\n"
        "hledger -f main.journal register expenses:dining -M")
    assert reply.requested == []
    assert reply.notices == []
    assert "```json" not in reply.message
    assert '"budget_changes"' not in reply.message


def test_talk_accepts_literal_newlines_in_model_json_strings():
    raw = '''```json
{"message": "First paragraph.

Second paragraph.", "budget_changes": []}
```'''

    reply = _parse_reply(raw, {"expenses:dining"})

    assert reply.message == "First paragraph.\n\nSecond paragraph."
    assert reply.notices == []


def test_talk_formats_inline_markdown_lists_for_the_terminal():
    message = (
        "The three biggest gaps are: 1. **Shopping** — $643 over budget. "
        "2. **Dining** — $307 over budget. 3. **Subscriptions** — $301 over "
        "budget. Combined, these are $1,251 over budget. "
        "**Practical suggestions:** - **Shopping**: Add a monthly cap. "
        "- **Dining**: Pick a weekly target. If you'd like, I can update the "
        "envelopes."
    )

    cleaned = _clean_message(message)

    assert "\n\n1. Shopping —" in cleaned
    assert "\n2. Dining —" in cleaned
    assert "\n3. Subscriptions —" in cleaned
    assert "\n\nCombined," in cleaned
    assert "\n\nPRACTICAL SUGGESTIONS\n" in cleaned
    assert "\n• Shopping:" in cleaned
    assert "\n• Dining:" in cleaned
    assert "\n\nIf you'd like" in cleaned
    assert "**" not in cleaned


def test_talk_warns_if_malformed_response_contains_a_write_action():
    raw = r'''{
      "message": "I can still explain the recommendation."
      "budget_changes": [
        {"category": "expenses:dining", "monthly_amount": 200}
      ]
    }'''

    reply = _parse_reply(raw, {"expenses:dining"})

    assert reply.message == "I can still explain the recommendation."
    assert reply.requested == []
    assert "ignored" in reply.notices[0]


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
        def __init__(self, height=30, width=120):
            self.height = height
            self.width = width
            self.writes = []

        def erase(self):
            pass

        def getmaxyx(self):
            return self.height, self.width

        def addstr(self, *args):
            self.writes.append(args)

        def move(self, *args):
            pass

        def refresh(self):
            pass

    screen = Screen()
    session = TalkSession(env.cfg)
    session.context = {
        "household": {"monthly_savings_target": 500.0},
        "current_month_to_date": {
            "month": "2026-08",
            "partial_through": "2026-08-27",
            "spend_to_date": 425.0,
            "income_to_date": 3000.0,
            "categories": [
                {"category": "expenses:dining", "monthly_budget": 400.0,
                 "spent_to_date": 425.0},
                {"category": "expenses:groceries", "monthly_budget": 650.0,
                 "spent_to_date": 300.0},
            ],
        },
    }
    tui = TalkTUI(screen, session)
    tui.transcript.append((
        "Budge", "TOP OPPORTUNITIES\n\n1. Shopping — This deliberately "
        "long recommendation wraps with a hanging indent for readability.\n"
        "• Set a monthly cap."))
    tui._draw()
    rendered = "\n".join(str(call[2]) for call in screen.writes)

    assert "BUDGE // TALK" in rendered
    assert "LIVE BUDGET" in rendered
    assert "PERSONAL FINANCE COMMAND DECK" in rendered
    assert "ENVELOPES" in rendered
    assert "dining" in rendered
    assert "████" in rendered
    assert "MESSAGE" in rendered
    heading = next(call for call in screen.writes
                   if "TOP OPPORTUNITIES" in str(call[2]))
    numbered = next(call for call in screen.writes
                    if "1. Shopping" in str(call[2]))
    prose = next(call for call in screen.writes
                 if "readability" in str(call[2]))
    assert heading[3] == tui.theme["purple"]
    assert numbered[3] == tui.theme["cyan"]
    assert prose[3] == tui.theme["text"]

    assert tui._command("/budget") is False
    assert tui.transcript[-1] == (
        "Budget", "expenses:dining: $400.00/month\n"
        "expenses:groceries: $650.00/month")

    narrow = Screen(height=24, width=80)
    TalkTUI(narrow, session)._draw()
    narrow_rendered = "\n".join(str(call[2]) for call in narrow.writes)
    assert "CONVERSATION" in narrow_rendered
    assert "LIVE BUDGET" not in narrow_rendered


def test_talk_tui_opens_long_new_response_at_its_first_line(env):
    _seed_budget(env)

    class Screen:
        def __init__(self):
            self.writes = []

        def erase(self):
            self.writes = []

        def getmaxyx(self):
            return 22, 100

        def addstr(self, *args):
            self.writes.append(args)

        def move(self, *args):
            pass

        def refresh(self):
            pass

    screen = Screen()
    tui = TalkTUI(screen, TalkSession(env.cfg))
    tui.transcript.extend([
        ("You", "Review my budget."),
        ("Budge", "OPENING LINE\n" + "\n".join(
            f"Detail line {number}" for number in range(1, 30))
         + "\nFINAL LINE"),
    ])
    tui._scroll_to_transcript = len(tui.transcript) - 1

    tui._draw()
    rendered = "\n".join(str(call[2]) for call in screen.writes)

    assert "OPENING LINE" in rendered
    assert "Detail line 1" in rendered
    assert "FINAL LINE" not in rendered
    assert tui.scroll_offset > 0

    # The focus becomes an ordinary scroll offset and survives redraws.
    tui._draw()
    rendered_again = "\n".join(str(call[2]) for call in screen.writes)
    assert "OPENING LINE" in rendered_again
