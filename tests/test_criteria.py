import copy
import io
import json

import pandas as pd
import pytest

from fuel_planner.analysis import assess_risk
from fuel_planner.data import ROOT, case_from_snapshot, case_snapshot, edit_case
from fuel_planner.engine import evaluate
from fuel_planner.models import Decisions, Order, Plan, Reservation, Risk
from fuel_planner.recovery import recovery_plan
from fuel_planner.sensitivity import experiment
from fuel_planner.optimizer import Settings, optimize
from fuel_planner.storage import export_bytes, load_plan, save_plan


def test_research_inputs_saved_and_protected(case, tmp_path):
    initial_hash = case.fingerprint
    demand = copy.deepcopy(list(case.demand.values()))
    sources = copy.deepcopy(list(case.sources.values()))
    demand[0]["base_total_t"] = 105
    sources[0]["variable_cost_mln_per_t"] = 7
    edited = edit_case(case, demand, sources, "Проверка цены и спроса")
    assert edited.fingerprint != initial_hash
    assert case.fingerprint == initial_hash
    p = Plan(plan_id="edited-case", case_snapshot=case_snapshot(edited))
    reopened = load_plan(save_plan(p, tmp_path))
    restored_case = case_from_snapshot(reopened.case_snapshot)
    assert restored_case.fingerprint == edited.fingerprint
    assert evaluate(restored_case, reopened).payload["yearly_balance"][0]["demand_t"] == 105
    bad = case_snapshot(edited)
    bad["constraints"][0]["value"] = .1
    with pytest.raises(ValueError, match="ограничения"):
        case_from_snapshot(bad)
    bad = case_snapshot(edited)
    bad["scenarios"]["MANDATORY_STRESS"]["actual_delivery_share"]["Lunar-ISRU"]["2038"] = 1
    with pytest.raises(ValueError, match="стресс"):
        case_from_snapshot(bad)
    demand[0]["base_total_t"] = -10
    with pytest.raises(ValueError):
        edit_case(case, demand, sources, "Некорректное значение")


def test_research_optimum_retains_its_input_snapshot(single_year):
    single_year.sources["A"]["variable_cost_mln_per_t"] = 7.0
    result = optimize(single_year, Settings(time_limit=10))
    assert result.validated
    assert result.plan.case_snapshot is not None
    restored = case_from_snapshot(result.plan.case_snapshot)
    assert restored.fingerprint == single_year.fingerprint
    assert evaluate(restored, result.plan).total_cost == pytest.approx(result.objective_mln)


def test_cost_partition_controls_and_stakeholder_cash(single_year, funded_plan):
    # Force an unused take-or-pay minimum, which must be separated, not added twice.
    funded_plan.decisions.capacity_reservations[0].annual_capacity_t = 190
    r = evaluate(single_year, funded_plan)
    f = r.payload["financial_breakdown"][0]
    assert f["take_or_pay_mln"] > 0
    assert f["procurement_mln"] == pytest.approx(f["fuel_purchasing_mln"]+f["take_or_pay_mln"])
    assert all(v["passed"] for v in r.payload["independent_controls"])
    assert len({s["stakeholder_id"] for s in r.payload["stakeholders"]}) == 6
    assert sum(s["allocated_cash_cost_mln"] for s in r.payload["stakeholders"]) == pytest.approx(r.total_cost)
    assert sum(s["attributed_receipts_mln"] for s in r.payload["stakeholders"]) == pytest.approx(f["procurement_mln"]+f["reservation_mln"])
    assert all("unit" in c and "passed" in c for c in r.payload["constraint_ledger"])
    empty = evaluate(single_year, Plan(plan_id="invalid-physical-plan"))
    assert all(v["year"] and v["message"] and v["unit"] and v["excess"] > 0 for v in empty.payload["constraint_checks"])


def test_mitigation_honors_committed_orders(single_year):
    single_year.sources["A"]["lead_time_min_value"] = 0
    single_year.sources["A"]["lead_time_max_value"] = 0
    p = Plan(plan_id="reaction", decisions=Decisions(capacity_reservations=[Reservation(source_id="A", year=2035, annual_capacity_t=40)], supply_orders=[
        Order(source_id="A", order_date="2035-01-01", delivery_date="2035-01-01", volume_t=20),
        Order(source_id="A", order_date="2035-03-01", delivery_date="2035-03-01", volume_t=20),
    ]))
    risk = Risk(risk_id="TEAM_TIMING", event="Недопоставка", source_id="A", start_year=2035, end_year=2035, delivery_share=.5, mitigation_decision_date="2035-01-01", reaction_days=42, mitigation_cost_mln=12)
    before, after = assess_risk(single_year, p, risk)
    assert [r["arrival_t"] for r in before.payload["source_schedule"]] == [10, 10]
    assert [r["arrival_t"] for r in after.payload["source_schedule"]] == [10, 20]
    assert after.payload["financial_breakdown"][0]["mitigation_mln"] == 12
    assert after.payload["risk_register"][0]["shortage_reduction_t"] > 0
    assert after.payload["risk_register"][0]["mitigation_cost_included"]


def test_response_preserves_commitments_capacity_and_headroom(case):
    original = load_plan(ROOT/"examples/plans/diversified.json")
    serialized = original.model_dump_json()
    adapted = recovery_plan(case, original)
    count = len(original.decisions.supply_orders)
    assert original.model_dump_json() == serialized
    assert adapted.decisions.supply_orders[:count] == original.decisions.supply_orders
    assert adapted.decisions.investments == original.decisions.investments
    assert adapted.decisions.capacity_reservations == original.decisions.capacity_reservations
    for order in adapted.decisions.supply_orders[count:]:
        assert order.order_date >= "2038-01-01"
        assert order.delivery_date >= "2038-02-12"
    result = evaluate(case, adapted, "MANDATORY_STRESS")
    assert result.feasible
    assert result.summary()["shortage_t"] < 1e-6
    assert result.summary()["rejected_t"] < 1e-6
    earth = evaluate(case, load_plan(ROOT/"examples/plans/earth.json"))
    assert evaluate(case, original).summary()["flexible_rights_2038_t"] > earth.summary()["flexible_rights_2038_t"]
    assert sum(f["mitigation_mln"] for f in result.payload["financial_breakdown"]) == 20
    assert len(adapted.decisions.supply_orders) > count
    adapted.decisions.supply_orders[-1].order_date = "2037-01-01"
    with pytest.raises(ValueError, match="дата|дату"):
        evaluate(case, adapted, "MANDATORY_STRESS")


def test_repeatability_and_large_excel_metadata(case):
    p = load_plan(ROOT/"examples/plans/diversified.json")
    first, second = evaluate(case, p), evaluate(case, p)
    assert export_bytes(first) == export_bytes(second)
    book = io.BytesIO(export_bytes(first, "xlsx"))
    metadata = pd.read_excel(book, sheet_name="metadata")
    chunks = metadata[metadata.field == "plan_snapshot"].sort_values("part")
    assert len(chunks) > 1
    reconstructed = json.loads("".join(chunks.value))
    assert reconstructed == p.model_dump()
    financial = pd.read_excel(book, sheet_name="financial_breakdown")
    assert financial.total_cost_mln.sum() == pytest.approx(first.total_cost)


def test_sensitivity_keeps_base_and_price_does_not_change_physics(case):
    p = load_plan(ROOT/"examples/plans/diversified.json")
    original_hash = case.fingerprint
    base, expensive = evaluate(case, p), experiment(case, p, "core_price", 1.5)
    assert expensive.total_cost > base.total_cost
    assert expensive.payload["inventory_trace"] == base.payload["inventory_trace"]
    assert case.fingerprint == original_hash
    reduced = experiment(case, p, "core_delivery", .5)
    assert reduced.summary()["shortage_t"] > base.summary()["shortage_t"]
    assert reduced.payload["scenario_id"] == "TEAM_SENSITIVITY"
    delayed = experiment(case, p, "core_delay", 30)
    delayed_launch = [s for s in delayed.payload["stakeholders"] if s["stakeholder_id"] == "launch_providers" and s["year"] >= 2038]
    assert any(s["kpi_value"] < 1 for s in delayed_launch)
