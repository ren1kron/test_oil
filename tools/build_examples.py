"""Generate evaluated participant strategies and exports without changing CASE_INPUT."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from fuel_planner.analysis import assess_risk, demand_threshold, sensitivity
from fuel_planner.data import ROOT, load_case, research_case
from fuel_planner.engine import evaluate
from fuel_planner.models import Risk
from fuel_planner.optimizer import Settings, optimize
from fuel_planner.recovery import reserve_flexibility
from fuel_planner.storage import export_bytes, save_plan


def main():
    case = load_case()
    output = ROOT / "results"
    output.mkdir(exist_ok=True)
    comparisons, metadata = [], {}
    for strategy, choices in [("earth", {"LUNAR_ISRU": "never"}), ("lunar", {"LUNAR_ISRU": 2037}), ("unrestricted", {})]:
        solution = optimize(case, Settings(plan_id=strategy, investments=choices))
        metadata[strategy] = solution.metadata()
        if not solution.plan:
            raise RuntimeError(f"{strategy}: {solution.message}")
        save_plan(solution.plan, ROOT / "examples" / "plans")
        print(strategy, solution.metadata(), flush=True)
        for scenario in ["BASE", "MANDATORY_STRESS", "LOW_DEMAND", "HIGH_DEMAND"]:
            result = evaluate(case, solution.plan, scenario)
            comparisons.append(result.summary())
            (output / f"{strategy}-{scenario}.zip").write_bytes(export_bytes(result, "csv"))
            if strategy == "earth" and scenario == "MANDATORY_STRESS":
                (output / "earth-stress.xlsx").write_bytes(export_bytes(result, "xlsx"))
        if strategy == "unrestricted":
            risk = Risk(risk_id="TEAM_CORE_DELAY", event="Earth-Core: задержка 30 дней и 20% недопоставки", source_id="A", start_year=2038, end_year=2039, delay_days=30, delivery_share=.8, residual_delay_days=7, mitigation="Резервное окно запуска и восстановление объема; затраты меры требуют отдельного обоснования")
            affected, residual = assess_risk(case, solution.plan, risk)
            (output / "risk-register.json").write_text(json.dumps(affected.payload["risk_register"], ensure_ascii=False, indent=2), encoding="utf-8")
            (output / "risk-analysis.zip").write_bytes(export_bytes(affected, "csv"))
            pd.DataFrame(sensitivity(case, solution.plan)).to_csv(output / "sensitivity.csv", index=False)
            (output / "demand-threshold.json").write_text(json.dumps(demand_threshold(case, solution.plan), indent=2), encoding="utf-8")
    diversified = optimize(case, Settings(plan_id="diversified", investments={"EARTH_NEW": 2035, "ZBO": 2036, "LUNAR_ISRU": 2037}))
    if diversified.plan is None:
        raise RuntimeError(diversified.message)
    metadata["diversified_before_flexibility_reservations"] = diversified.metadata()
    save_plan(reserve_flexibility(case, diversified.plan), ROOT / "examples" / "plans")
    extended = research_case(case, extra_source=True, future_year=True)
    extension = optimize(extended, Settings(plan_id="research-extension"))
    metadata["research-extension"] = extension.metadata()
    if not extension.plan:
        raise RuntimeError(extension.message)
    save_plan(extension.plan, ROOT / "examples" / "research_plans")
    result = evaluate(extended, extension.plan)
    (output / "research-extension.zip").write_bytes(export_bytes(result, "csv"))
    (output / "research-extension.json").write_text(json.dumps(dict(summary=result.summary(), assumptions=extended.assumptions, source_x_delivered_t=sum(r["delivered_t"] for r in result.payload["source_schedule"] if r["source_id"] == "X")), ensure_ascii=False, indent=2), encoding="utf-8")
    metadata["input_fingerprint"] = case.fingerprint
    (output / "optimizer.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(comparisons).to_csv(output / "comparison.csv", index=False)
    print("Examples and exports written to", output, flush=True)
    from tools.audit_submission import main as audit
    audit()


if __name__ == "__main__":
    main()
