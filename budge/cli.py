"""budge CLI — orchestration only, never a wrapper around hledger.

Subcommands exist ONLY for workflows hledger does not have (PRD section 1):
fetch, categorize, review/promote, plan, setup, push, notify, regenerate.
There is deliberately no `budge balance` and no `budge report` — reporting,
queries, and validation are hledger commands the operator runs directly:

    hledger -f main.journal balance --budget -M expenses
    hledger -f main.journal check
"""

from __future__ import annotations

import argparse
import sys

from . import util
from .config import Config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="budge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print intended actions; write nothing (A12)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="interactive bootstrap (PRD 7.1)")
    p.add_argument("--services-only", action="store_true",
                   help="just (re)render and install systemd units + the "
                        "Paisa dashboard from existing config; no prompts")

    p = sub.add_parser("fetch", help="SimpleFIN pull + rules-first import "
                                     "(PRD 7.2)")
    p.add_argument("--backfill", type=int, metavar="DAYS",
                   help="backfill mode: also write opening balances")

    sub.add_parser("categorize", help="AI suggestions for unmatched txns "
                                      "(PRD 7.4)")

    p = sub.add_parser("review", help="vendor-grouped review (PRD 7.5)")
    p.add_argument("--edit", action="store_true",
                   help="open pending.journal in $EDITOR")

    sub.add_parser("promote", help="check-gated promote of all pending "
                                   "(PRD 7.5)")

    p = sub.add_parser("plan", help="budget planning wizard (PRD 7.8)")
    p.add_argument("--bootstrap", action="store_true")
    p.add_argument("--reassess", action="store_true")
    p.add_argument("--months", type=int, default=3)
    p.add_argument("--from-setup", action="store_true",
                   help=argparse.SUPPRESS)

    sub.add_parser("regenerate", help="rebuild pending.journal from raw "
                                      "CSVs + rules + decision log")

    sub.add_parser("push", help="git push the data repo")

    p = sub.add_parser("notify", help="OpenClaw notifications (PRD 7.7)")
    p.add_argument("--unit", help="failed unit name (OnFailure hook)")
    p.add_argument("--review-nudge", action="store_true")

    args = parser.parse_args(argv)
    util.DRY_RUN = args.dry_run
    cfg = Config()

    try:
        if args.command == "setup":
            from .setup_cmd import run_setup
            run_setup(cfg, services_only=args.services_only)
        elif args.command == "fetch":
            from .fetch import run_fetch
            run_fetch(cfg, backfill_days=args.backfill,
                      interactive=bool(args.backfill))
        elif args.command == "categorize":
            from .categorize import run_categorize
            run_categorize(cfg)
        elif args.command == "review":
            from .review import run_review
            run_review(cfg, edit=args.edit)
        elif args.command == "promote":
            from .review import promote
            promote(cfg)
        elif args.command == "plan":
            from .plan import run_plan
            mode = ("bootstrap" if args.bootstrap
                    else "reassess" if args.reassess else None)
            run_plan(cfg, mode=mode, months=args.months,
                     from_setup=args.from_setup)
        elif args.command == "regenerate":
            from .categorize import regenerate
            result = regenerate(cfg)
            print(f"regenerated: {result['promoted_by_rule']} rule-matched "
                  f"moved to main, {result['kept']} kept pending")
        elif args.command == "push":
            from .gitutil import push
            push(cfg.repo)
        elif args.command == "notify":
            from .notify import notify_failure, notify_review_ready
            if args.review_nudge:
                notify_review_ready(cfg)
            elif args.unit:
                notify_failure(cfg, args.unit)
            else:
                print("notify: need --unit or --review-nudge",
                      file=sys.stderr)
                return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # budge-specific operational errors get a clean message, not a
        # traceback; anything else is a genuine bug and should crash loudly.
        from .ai import AIError
        from .simplefin import SimpleFINError
        if isinstance(e, (AIError, SimpleFINError)):
            print(f"error: {e}", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
