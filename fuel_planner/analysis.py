from __future__ import annotations

from .data import Case
from .engine import evaluate
from .models import Plan, Risk


def compare(case: Case, plan: Plan):
    return [evaluate(case, plan, scenario).summary() for scenario in ["BASE", "MANDATORY_STRESS", "LOW_DEMAND", "HIGH_DEMAND"]]


def assess_risk(case: Case, plan: Plan, risk: Risk):
    base = evaluate(case, plan)
    affected = evaluate(case, plan, risk.risk_id, risk)
    residual_risk = risk.model_copy(update={"mitigation_active": True})
    residual = evaluate(case, plan, residual_risk.risk_id, residual_risk)
    row = risk.model_dump()
    row.update(plan_id=plan.plan_id, input_fingerprint=case.fingerprint, plan_fingerprint=base.payload["plan_fingerprint"],
               affected_parameter="delivery_share / delivery_delay / variable_price", period=f"{risk.start_year}–{risk.end_year}",
               financial_consequence_mln=affected.total_cost-base.total_cost,
               physical_consequence_t=affected.summary()["shortage_t"]-base.summary()["shortage_t"],
               service_consequence=affected.summary()["min_total_service"]-base.summary()["min_total_service"],
               residual_shortage_t=residual.summary()["shortage_t"],
               residual_cost_delta_mln=residual.total_cost-base.total_cost,
               mitigation_cost_included=True, mitigation_cost_mln=risk.mitigation_cost_mln,
               shortage_reduction_t=affected.summary()["shortage_t"]-residual.summary()["shortage_t"],
               critical_shortage_reduction_t=affected.summary()["critical_shortage_t"]-residual.summary()["critical_shortage_t"],
               mitigation_net_cost_mln=residual.total_cost-affected.total_cost,
               decision_date=risk.mitigation_decision_date or f"{risk.start_year}-01-01",
               commitment_rule="Only orders placed after decision + reaction_days change; no historical deliveries or commitments rewritten")
    affected.payload["risk_register"] = [row]
    residual.payload["risk_register"] = [row]
    return affected, residual


def sensitivity(case: Case, plan: Plan, scales=(0.8, 1.0, 1.1, 1.15, 1.25, 1.5)):
    return [dict(demand_multiplier=scale, **evaluate(case, plan, demand_scale=scale).summary()) for scale in scales]


def demand_threshold(case: Case, plan: Plan, upper=2.0, iterations=16):
    def failed(scale):
        r = evaluate(case, plan, demand_scale=scale)
        return any(c["rule_id"] in {"RESERVE_45D", "BASE_TOTAL_SERVICE", "BASE_CRITICAL_SERVICE"} for c in r.payload["constraint_checks"])
    if failed(1.0):
        return {"status": "already_failing", "multiplier": 1.0}
    if not failed(upper):
        return {"status": "not_found", "tested_through": upper}
    low, high = 1.0, upper
    for _ in range(iterations):
        mid = (low+high)/2
        if failed(mid):
            high = mid
        else:
            low = mid
    return {"status": "found", "last_passing": low, "first_failing": high, "resolution": high-low}
