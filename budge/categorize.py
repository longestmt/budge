"""AI categorizer (PRD section 7.4) and pending.journal regeneration.

Write surface: pending.journal and ai/ai-decisions.log ONLY. The categorizer
never touches main.journal, the rules files, or git history (it does not even
commit — the next fetch or promote commit picks its output up).

Regeneration (PRD section 4): pending.journal is a derived artifact. It is
rebuilt from raw CSVs + rules + the decision log. Entries that NOW match a
rule (after a review correction) leave pending and enter main.journal cleared,
exactly as they would have at import — so a corrected vendor never re-enters
pending (acceptance A7).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ai, ailog, fetch, hledger, journal
from .scaffold import load_accounts
from .util import die, dry, say, warn

BATCH = 25


def allowed_categories(repo: Path) -> set:
    """Expense/income/liability categories declared in accounts.journal."""
    cats = set()
    path = Path(repo) / "accounts.journal"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split(";")[0].strip()
            if line.startswith("account "):
                cats.add(line[len("account "):].strip())
    cats.add(journal.UNCATEGORIZED)
    cats.add(journal.TRANSFERS)
    return cats


def assemble_system_prompt(cfg) -> str:
    """agent.md + live category list + recently approved examples."""
    repo = cfg.repo
    template = (Path(repo) / "ai" / "agent.md").read_text(encoding="utf-8")
    cats = sorted(
        c for c in allowed_categories(repo)
        if c.split(":")[0] in ("expenses", "income")
        and c != journal.UNCATEGORIZED
    )
    examples = ailog.recent_accepted_examples(repo)
    example_text = "\n".join(f"- {p!r} -> {c}" for p, c in examples) \
        or "(none yet)"
    return (template
            .replace("{{CATEGORIES}}", "\n".join(f"- {c}" for c in cats))
            .replace("{{EXAMPLES}}", example_text))


def run_categorize(cfg) -> int:
    """Suggest categories for every uncategorized pending transaction."""
    repo = cfg.repo
    entries = journal.parse_pending(repo / "pending.journal")
    todo = [e for e in entries if e.category == journal.UNCATEGORIZED]
    if not todo:
        say("nothing to categorize")
        return 0
    system = assemble_system_prompt(cfg)
    cats = allowed_categories(repo)
    raw_rows = fetch.all_raw_rows(repo)
    done = 0

    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        payload = ai.minimized_payload([
            {
                "id": e.sf_id,
                "date": e.date,
                "payee": e.payee,
                "description": raw_rows.get(e.sf_id, ("", {}))[1].get("memo", ""),
                "amount": e.amount,
                "source_account": e.source_account,
            }
            for e in batch
        ])
        user = json.dumps({"transactions": payload}, ensure_ascii=False)
        if dry(f"AI categorize batch of {len(batch)} txns"):
            continue
        try:
            reply = ai.complete(cfg, system, user)
        except ai.AIError as e:
            warn(f"AI provider failed; transactions stay uncategorized "
                 f"and will be retried next run: {e}")
            break
        parsed = ai.extract_json(reply)
        decisions = {}
        if isinstance(parsed, dict) and isinstance(
                parsed.get("transactions"), list):
            for d in parsed["transactions"]:
                if isinstance(d, dict) and d.get("id"):
                    decisions[str(d["id"])] = d
        for entry in batch:
            d = decisions.get(entry.sf_id)
            valid, reason = (None, "missing from model output") if d is None \
                else ai.validate_decision(d, cats)
            if valid is None:
                # Malformed output is rejected, never guessed at (PRD 7.4/A9)
                ailog.append(
                    repo, event="reject", txn_id=entry.sf_id,
                    payee=entry.payee, amount=entry.amount,
                    model=cfg.ai_model, reason=reason,
                )
                continue
            entry.category = valid["category"]
            entry.confidence = valid["confidence"]
            entry.rationale = valid["rationale"]
            entry.origin = "ai"
            entry.suggested = valid["category"]
            ailog.append(
                repo, event="suggest", txn_id=entry.sf_id, payee=entry.payee,
                amount=entry.amount, suggestion=valid["category"],
                confidence=valid["confidence"], rationale=valid["rationale"],
                model=cfg.ai_model, transfer=valid["transfer"],
            )
            done += 1

    journal.write_pending(repo / "pending.journal", entries)
    ok, output = hledger.check(repo / "main.journal")
    if not ok:
        die("hledger check failed after categorization:\n" + output)
    say(f"categorized {done} of {len(todo)} pending transactions")
    return done


def regenerate(cfg) -> dict:
    """Rebuild pending.journal from raw CSVs + rules + decision log.

    Precedence per transaction:
      rule match (leaves pending -> main.journal, cleared)
      > operator manual override (stays pending, origin=manual)
      > latest AI suggestion (stays pending, origin=ai)
      > uncategorized (stays pending)
    """
    repo = cfg.repo
    entries = journal.parse_pending(repo / "pending.journal")
    if not entries:
        return {"promoted_by_rule": 0, "kept": 0}
    raw_rows = fetch.all_raw_rows(repo)
    overrides = ailog.manual_overrides(repo)
    suggestions = ailog.latest_suggestions(repo)
    accounts = {a["slug"]: a for a in load_accounts(repo)}

    by_slug = {}
    main_texts, kept = [], []
    for e in entries:
        slug, row = raw_rows.get(e.sf_id, (None, None))
        if slug is None:
            warn(f"pending txn {e.sf_id} not found in raw CSVs; keeping as-is")
            kept.append(e)
            continue
        by_slug.setdefault(slug, []).append((e, row))
    outcome_events = []
    for slug, pairs in by_slug.items():
        converted = fetch.convert_rows(repo, slug, [row for _, row in pairs])
        source = accounts.get(slug, {}).get(
            "account", pairs[0][0].source_account)
        for entry, row in pairs:
            text = converted.get(entry.sf_id)
            category = fetch.entry_category(text, source) if text else ""
            if text and category and category != journal.UNCATEGORIZED:
                main_texts.append(text)  # rule absorbed this vendor
                result = ("accepted" if entry.suggested == category
                          else "overridden")
                if entry.suggested:
                    outcome_events.append(dict(
                        event="outcome", txn_id=entry.sf_id,
                        suggestion=entry.suggested, final=category,
                        result=result, via="rule",
                    ))
                continue
            if entry.sf_id in overrides:
                entry.category = overrides[entry.sf_id]
                entry.origin = "manual"
            elif entry.sf_id in suggestions:
                s = suggestions[entry.sf_id]
                entry.category = s.get("suggestion", journal.UNCATEGORIZED)
                entry.confidence = s.get("confidence", "")
                entry.rationale = s.get("rationale", "")
                entry.origin = "ai"
                entry.suggested = entry.category
            else:
                entry.category = journal.UNCATEGORIZED
                entry.origin = "uncategorized"
            kept.append(entry)

    fetch.append_main(repo, main_texts)
    journal.write_pending(repo / "pending.journal", kept)
    ok, output = hledger.check(repo / "main.journal")
    if not ok:
        die("hledger check failed after regeneration:\n" + output)
    for ev in outcome_events:
        ailog.append(repo, **ev)
    return {"promoted_by_rule": len(main_texts), "kept": len(kept)}
