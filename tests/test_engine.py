import copy
import json

import pytest

from fuel_planner.data import ROOT, research_case, validate_case
from fuel_planner.engine import balance, capacity_excess, evaluate, payable_volume, reservation_payment, reserve_t, serve
from fuel_planner.models import Decisions, Investment, Order, Plan, Reservation, Risk, day_index, day_label, lead_days


def test_organizer_vectors(single_year):
    actual = {
        "V01": {"closing_inventory_t": balance(10, 30, 2, 25)},
        "V02": {"served_t": serve(8, 10, 0)[0], "shortage_t": serve(8, 10, 0)[2], "closing_inventory_t": serve(8, 10, 0)[3]},
        "V03": {"payable_volume_t": payable_volume(50, 100, .7), "variable_payment_mln": payable_volume(50, 100, .7)*2},
        "V04": {"variable_payment_mln": payable_volume(50, 100, .7)*2},
        "V05": {"reservation_payment_mln": reservation_payment(.4, 100, .5)},
        "V07": {"reserve_t": reserve_t(365)},
        "V08": {"violation": "CAPACITY_EXCEEDED", "excess_t": capacity_excess(12, 10)},
    }
    c = copy.deepcopy(single_year)
    c.storage["BASE"]["loss_rate_on_throughput"] = .05
    c.demand[2035]["base_critical_t"] = 60
    p = Plan(plan_id="vectors", decisions=Decisions(supply_orders=[Order(source_id="A", order_date="2034-01-01", delivery_date="2035-01-01", volume_t=20)], capacity_reservations=[Reservation(source_id="A", year=2035, annual_capacity_t=100)]))
    r = evaluate(c, p)
    actual["V06"] = {"losses_t": r.payload["yearly_balance"][0]["losses_t"]}
    actual["V09"] = {"total_demand_t": r.payload["yearly_balance"][0]["demand_t"]}
    c.sources["A"]["reliability_profile"] = "constant:0.80"
    risk = Risk(risk_id="TEAM_VECTOR", event="vector", source_id="A", start_year=2035, end_year=2035, delivery_share=.5)
    r = evaluate(c, p, risk.risk_id, risk)
    actual["V10"] = {"actual_delivery_t": r.payload["source_schedule"][0]["arrival_t"]}
    for vector in json.loads((ROOT / "validation/expected_checks.json").read_text()):
        assert actual[vector["case_id"]] == vector["expected"]


def test_daily_conservation_and_funded_start(single_year, funded_plan):
    r = evaluate(single_year, funded_plan)
    assert r.feasible
    year = r.payload["yearly_balance"][0]
    assert year["total_service_level"] == pytest.approx(1)
    assert year["reserve_opening_t"] == pytest.approx(reserve_t(100))
    assert year["opening_inventory_t"] == 0
    assert year["delivered_t"] == pytest.approx(sum(o.volume_t for o in funded_plan.decisions.supply_orders))
    for row in r.payload["inventory_trace"]:
        assert row["closing_inventory_t"] == pytest.approx(balance(row["opening_inventory_t"], row["delivered_t"], row["losses_t"], row["served_t"]))
        assert 0 <= row["closing_inventory_t"] <= row["capacity_t"]
    assert r.total_cost > sum(o.volume_t*6.2 for o in funded_plan.decisions.supply_orders)


def test_shortage_priority_zero_demand_and_reliability(single_year):
    empty = evaluate(single_year, Plan(plan_id="empty"))
    assert empty.payload["yearly_balance"][0]["shortage_t"] == pytest.approx(100)
    assert all(r["closing_inventory_t"] == 0 for r in empty.payload["inventory_trace"])
    assert serve(7, 10, 6) == (7, 6, 3, 0)
    single_year.demand[2035].update(base_total_t=0, base_critical_t=0)
    assert evaluate(single_year, Plan(plan_id="zero")).payload["yearly_balance"][0]["total_service_level"] == 1


def test_timing_and_overflow(single_year, funded_plan):
    p = funded_plan.model_copy(deep=True)
    p.decisions.supply_orders[0].order_date = "2035-01-01"
    r = evaluate(single_year, p)
    assert "LEAD_TIME" in {c["rule_id"] for c in r.payload["constraint_checks"]}
    assert r.payload["source_schedule"][0]["delivered_t"] == 0
    p = funded_plan.model_copy(deep=True)
    p.decisions.supply_orders[0].volume_t = 200
    r = evaluate(single_year, p)
    assert "STORAGE_OVERFLOW" in {c["rule_id"] for c in r.payload["constraint_checks"]}
    assert r.payload["yearly_balance"][0]["rejected_t"] > 0
    assert max(v["closing_inventory_t"] for v in r.payload["inventory_trace"]) <= 70


def test_investment_accounting_and_partial_opex(case):
    p = Plan(plan_id="capex", decisions=Decisions(investments=[Investment(investment_id="EARTH_NEW", option_date="2035-01-01", decision_date="2036-01-01"), Investment(investment_id="ZBO", decision_date="2036-07-01")]))
    r = evaluate(case, p)
    costs = {v["year"]: v for v in r.payload["financial_breakdown"]}
    assert costs[2035]["capex_mln"] == 90
    assert costs[2036]["capex_mln"] == 450
    assert sum(v["capex_mln"] for v in costs.values()) == 540
    assert costs[2036]["fixed_opex_mln"] == pytest.approx(12*184/365)


def test_full_mandatory_stress(case):
    p = Plan(plan_id="stress", decisions=Decisions(investments=[Investment(investment_id="LUNAR_ISRU", decision_date="2037-01-01")],
        capacity_reservations=[Reservation(source_id=s, year=y, annual_capacity_t=20) for s in ["A", "B", "D"] for y in [2038, 2039, 2040]],
        supply_orders=[Order(source_id=s, order_date=f"{y-1}-12-01" if s != "D" else f"{y}-01-01", delivery_date=f"{y}-12-01", volume_t=20) for s in ["A", "B", "D"] for y in [2038, 2039, 2040]]))
    base, stress = evaluate(case, p), evaluate(case, p, "MANDATORY_STRESS")
    for year, factor in [(2038, .55), (2039, .75), (2040, 1)]:
        rec = next(r for r in stress.payload["source_schedule"] if r["source_id"] == "D" and r["year"] == year)
        assert rec["arrival_t"] == pytest.approx(20*factor)
    for b, s in zip(base.payload["yearly_balance"], stress.payload["yearly_balance"]):
        assert s["demand_t"] == pytest.approx(b["demand_t"]*(1.15 if b["year"] >= 2038 else 1))
    for b, s in zip(base.payload["financial_breakdown"], stress.payload["financial_breakdown"]):
        assert s["reservation_mln"] == b["reservation_mln"]
        assert s["capex_mln"] == b["capex_mln"]
        expected_shock = (20*6.2+20*8.9)*.25 if b["year"] in [2038, 2039] else 0
        assert s["procurement_mln"]-b["procurement_mln"] == pytest.approx(expected_shock)
    assert {v["period"] for v in stress.payload["constraint_checks"] if v["rule_id"] == "STRESS_LOSS_LIMIT"} == {"2038", "2039", "2040"}


def test_partial_contract_and_invalid_semantics(single_year):
    p = Plan(plan_id="partial", decisions=Decisions(capacity_reservations=[Reservation(source_id="A", year=2035, annual_capacity_t=100, start_day=100, end_day=200)]))
    r = evaluate(single_year, p)
    assert r.payload["financial_breakdown"][0]["reservation_mln"] == pytest.approx(.45*100*100/365)
    assert r.payload["financial_breakdown"][0]["procurement_mln"] == pytest.approx(.7*100*100/365*6.2)
    p.decisions.capacity_reservations *= 2
    with pytest.raises(ValueError, match="один договор"):
        evaluate(single_year, p)
    with pytest.raises(ValueError):
        evaluate(single_year, Plan(plan_id="x"), "BAD")


def test_calendar_and_extensions(case):
    assert lead_days(6, "week") == 42
    assert lead_days(24, "month") == 730
    assert day_label(day_index("2036-03-01")) == "2036-03-01"
    assert day_index("2036-03-01")-day_index("2036-02-28") == 1
    with pytest.raises(ValueError):
        day_index("2036-02-29")
    extended = research_case(case, True, True)
    assert "X" not in case.sources and "X" in extended.sources
    assert extended.demand[2041]["base_total_t"] == pytest.approx(409.5)
    result = evaluate(extended, Plan(plan_id="extension"))
    assert len(result.payload["inventory_trace"]) == 7*365
    assert extended.fingerprint != case.fingerprint


def test_emergency_cannot_bypass_streak(case):
    p = Plan(plan_id="emergency", decisions=Decisions(capacity_reservations=[Reservation(source_id="E", year=y, annual_capacity_t=1) for y in [2035, 2036, 2037]], supply_orders=[Order(source_id="E", order_date=f"{y}-01-01", delivery_date=f"{y}-06-01", volume_t=1) for y in [2035, 2036, 2037]]))
    assert any(c["rule_id"] == "EMERGENCY_BASE_STREAK" for c in evaluate(case, p).payload["constraint_checks"])
    p.decisions.supply_orders[0].contingency = True
    with pytest.raises(ValueError, match="Контингентная"):
        evaluate(case, p)


def test_invalid_case_and_future_bounds(case):
    bad = copy.deepcopy(case)
    bad.scenarios["BASE"]["demand_multiplier"]["default"] = -1
    with pytest.raises(ValueError, match="множитель"):
        validate_case(bad)
    extended = research_case(case, future_year=True)
    result = evaluate(extended, Plan(plan_id="future"), "MANDATORY_STRESS")
    assert not any(c["rule_id"] in {"STRESS_LOSS_LIMIT", "CAPEX_2040", "CAPEX_2037"} and c["period"] == "2041" for c in result.payload["constraint_checks"])
