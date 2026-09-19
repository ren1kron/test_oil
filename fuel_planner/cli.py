from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analysis import compare
from .data import ROOT, load_case, research_case, case_from_snapshot
from .engine import evaluate
from .optimizer import Settings, optimize
from .storage import export_bytes, load_plan, save_plan


def main():
    parser = argparse.ArgumentParser(description="Планирование орбитального топливного узла")
    parser.add_argument("--case", type=Path, default=ROOT)
    parser.add_argument("--extra-source", action="store_true")
    parser.add_argument("--future-year", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    opt = sub.add_parser("optimize")
    opt.add_argument("--strategy", choices=["earth", "lunar", "unrestricted"], default="unrestricted")
    opt.add_argument("--time-limit", type=float, default=120)
    opt.add_argument("--output", type=Path, default=ROOT / "saved_plans")
    for name in ["evaluate", "compare"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("plan", type=Path)
        cmd.add_argument("--output", type=Path)
        if name == "evaluate":
            cmd.add_argument("--scenario", default="BASE")
            cmd.add_argument("--format", choices=["json", "csv", "xlsx"], default="json")
    args = parser.parse_args()
    try:
        case = research_case(load_case(args.case), args.extra_source, args.future_year)
        if args.command == "optimize":
            choices = {"LUNAR_ISRU": "never"} if args.strategy == "earth" else {"LUNAR_ISRU": 2037} if args.strategy == "lunar" else {}
            result = optimize(case, Settings(time_limit=args.time_limit, investments=choices, plan_id=args.strategy))
            print(json.dumps(result.metadata(), ensure_ascii=False, indent=2))
            if result.plan:
                print(save_plan(result.plan, args.output))
            else:
                return 2
        elif args.command == "compare":
            plan = load_plan(args.plan)
            if plan.case_snapshot is not None:
                case = case_from_snapshot(plan.case_snapshot)
            rows = compare(case, plan)
            text = json.dumps(rows, ensure_ascii=False, indent=2)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(text, encoding="utf-8")
            print(text)
        else:
            plan = load_plan(args.plan)
            if plan.case_snapshot is not None:
                case = case_from_snapshot(plan.case_snapshot)
            result = evaluate(case, plan, args.scenario)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(export_bytes(result, args.format))
            print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
