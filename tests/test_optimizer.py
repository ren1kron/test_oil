import pytest

from fuel_planner.engine import evaluate
from fuel_planner.optimizer import Settings, optimize


def test_analytic_one_year_optimum(single_year):
    c = single_year
    # Initial reserve may be consumed after the initial check; no terminal reserve.
    expected_gross = 100/.955
    expected_holding = sum(max(0, 100*45/365 - 100/365*d) + max(0, 100*45/365 - 100/365*(d+1)) for d in range(45))*.5*.72/365
    # Subsequent just-in-time deliveries hold half a day's demand.
    expected_holding += 320 * (100/365/2)*.72/365
    expected_cost = expected_gross*(6.2+.45) + expected_holding
    solved = optimize(c, Settings(time_limit=10))
    assert solved.validated, solved.message
    assert solved.objective_mln == pytest.approx(expected_cost, abs=1e-5)
    assert solved.lower_bound_mln == pytest.approx(solved.objective_mln, abs=1e-5)
    assert evaluate(c, solved.plan).summary()["shortage_t"] < 1e-6


def test_optimizer_infeasible_and_time_limit(single_year):
    r = optimize(single_year, Settings(disabled_sources=["A"], time_limit=10))
    assert r.status == "infeasible"
    assert r.plan is None
    r = optimize(single_year, Settings(time_limit=1e-6))
    assert r.status in {"time_limit", "optimal_within_gap"}
    if r.plan:
        assert r.validated


def test_full_case_cost_reconciliation(case):
    r = optimize(case, Settings(time_limit=15, investments={"EARTH_NEW": 2035, "ZBO": 2036, "LUNAR_ISRU": "never"}))
    assert r.validated, r.message
    evaluated = evaluate(case, r.plan)
    assert evaluated.feasible
    assert evaluated.total_cost == pytest.approx(r.objective_mln, abs=1e-5)
    assert all(y["shortage_t"] < 1e-6 for y in evaluated.payload["yearly_balance"])
