"""One-factor experiments on copies, with explicit bounds and failure brackets."""
from __future__ import annotations

import copy

from .engine import evaluate
from .models import Risk

PARAMETERS = {
    "demand": dict(values=[.8, 1, 1.1, 1.15, 1.25, 1.5, 2], adverse=(1, 2), basis="0.8–1.25 из low/high данных; ×1.5–2 только TEAM reverse-stress, не прогноз"),
    "core_price": dict(values=[.8, 1, 1.25, 1.5, 2], adverse=(1, 2), basis="+25% как контроль масштаба из mandatory; остальные границы TEAM_ASSUMPTION, без вероятности"),
    "core_delivery": dict(values=[1, .9, .75, .5, 0], adverse=(1, 0), basis="TEAM: 0–100% исполнение ключевого земного канала; весь диапазон определен физически"),
    "core_delay": dict(values=[0, 7, 14, 30, 42, 90, 180], adverse=(0, 180), basis="TEAM: 7–180 дней; 42 дня — масштаб Emergency lead time, не оценка вероятности"),
}


def experiment(case, plan, parameter, value):
    c = copy.deepcopy(case)
    if parameter == "demand":
        return evaluate(c, plan, demand_scale=value)
    if parameter == "core_price":
        c.sources["A"]["variable_cost_mln_per_t"] *= value
        c.assumptions.append(f"TEAM_SENSITIVITY: Earth-Core price ×{value}")
        return evaluate(c, plan)
    risk = Risk(risk_id="TEAM_SENSITIVITY", event=parameter, source_id="A", start_year=min(max(2038, c.years[0]), c.years[-1]), end_year=c.years[-1],
                delay_days=int(value) if parameter == "core_delay" else 0,
                delivery_share=value if parameter == "core_delivery" else 1)
    return evaluate(c, plan, risk.risk_id, risk)


def study(case, plans, selected_id):
    records, thresholds = [], []
    for param, config in PARAMETERS.items():
        for value in config["values"]:
            evaluated = []
            for plan in plans:
                r = experiment(case, plan, param, value)
                record = dict(parameter=param, value=value, range_basis=config["basis"], baseline_input_fingerprint=case.fingerprint,
                              experiment_input_fingerprint=r.payload["input_fingerprint"], **r.summary())
                record["service_compliant"] = record["min_total_service"] >= .97-1e-6 and record["min_critical_service"] >= .99-1e-6
                evaluated.append(record)
            eligible = [r for r in evaluated if r["feasible"] and r["service_compliant"]]
            winner = min(eligible, key=lambda r: (r["total_cost_mln"], r["plan_id"]))["plan_id"] if eligible else "none_feasible"
            for row in evaluated:
                row["cheapest_compliant_plan"] = winner
            records.extend(evaluated)
        selected = next(p for p in plans if p.plan_id == selected_id)
        def failed(value):
            r = experiment(case, selected, param, value)
            return not r.feasible or r.summary()["min_total_service"] < .97-1e-6 or r.summary()["min_critical_service"] < .99-1e-6
        start, end = config["adverse"]
        if failed(start):
            thresholds.append(dict(parameter=param, status="already_failing", last_passing=None, first_failing=start, basis=config["basis"]))
        else:
            # Scan first to avoid asserting global monotonicity for storage constraints.
            points = [start+(end-start)*i/12 for i in range(13)]
            bracket = next(((a, b) for a, b in zip(points, points[1:]) if failed(b)), None)
            if bracket is None:
                thresholds.append(dict(parameter=param, status="not_found_in_range", last_passing=end, first_failing=None, basis=config["basis"]))
            else:
                low, high = bracket
                for _ in range(12):
                    mid = (low+high)/2
                    if failed(mid): high = mid
                    else: low = mid
                thresholds.append(dict(parameter=param, status="found", last_passing=low, first_failing=high, basis=config["basis"]))
        # Locate any cost-ranking crossover in the scanned intervals; do not invent one.
        baseline = [r for r in records if r["parameter"] == param and r["plan_id"] == selected_id]
        for left, right in zip(baseline, baseline[1:]):
            if left["cheapest_compliant_plan"] != right["cheapest_compliant_plan"]:
                thresholds.append(dict(parameter=param, status="ranking_change_bracket", last_passing=left["value"], first_failing=right["value"],
                                       basis=f"Изменение лучшего допустимого плана: {left['cheapest_compliant_plan']} → {right['cheapest_compliant_plan']}; интервал сетки"))
    return records, thresholds
