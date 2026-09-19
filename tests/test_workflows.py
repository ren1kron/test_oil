import io
import json
import zipfile

import pandas as pd
import pytest
from jsonschema import validate

from fuel_planner.analysis import assess_risk, compare, demand_threshold
from fuel_planner.data import ROOT
from fuel_planner.engine import evaluate
from fuel_planner.models import Plan, Risk
from fuel_planner.planning import generate_schedule
from fuel_planner.storage import export_bytes, load_plan, save_plan


def test_plan_roundtrip_and_exports(tmp_path, single_year, funded_plan):
    path = save_plan(funded_plan, tmp_path)
    restored = load_plan(path)
    assert restored == funded_plan
    r = evaluate(single_year, restored)
    validate(r.payload, json.loads((ROOT/"schemas/export.schema.json").read_text()))
    validate(restored.model_dump(), json.loads((ROOT/"schemas/plan.schema.json").read_text()))
    assert json.loads(export_bytes(r))["financial_breakdown"] == r.payload["financial_breakdown"]
    frame = pd.read_excel(io.BytesIO(export_bytes(r, "xlsx")), sheet_name="financial_breakdown")
    assert frame.total_cost_mln.sum() == pytest.approx(r.total_cost)
    with zipfile.ZipFile(io.BytesIO(export_bytes(r, "csv"))) as z:
        assert "metadata.json" in z.namelist()
        csv = pd.read_csv(z.open("yearly_balance.csv"))
        assert csv.delivered_t.sum() == pytest.approx(r.payload["yearly_balance"][0]["delivered_t"])


def test_invalid_plan_inputs():
    for payload in ['{"plan_id":"../escape"}', '{"plan_id":"x","decisions":{"supply_orders":[{"source_id":"A","order_date":"bad"}]}}']:
        with pytest.raises(ValueError):
            Plan.model_validate_json(payload)


def test_extra_source_uses_same_calculation(single_year, funded_plan):
    single_year.sources["X"] = dict(single_year.sources.pop("A"), source_id="X", name="Source-X")
    for order in funded_plan.decisions.supply_orders:
        order.source_id = "X"
    for reservation in funded_plan.decisions.capacity_reservations:
        reservation.source_id = "X"
    result = evaluate(single_year, funded_plan)
    assert result.feasible
    assert sum(row["delivered_t"] for row in result.payload["source_schedule"] if row["source_id"] == "X") > 100


def test_risks_sensitivity_and_schedule(single_year, funded_plan):
    risk = Risk(risk_id="TEAM_FAILURE", event="Недопоставка", source_id="A", start_year=2035, end_year=2035, delivery_share=.5)
    affected, residual = assess_risk(single_year, funded_plan, risk)
    assert affected.summary()["shortage_t"] > residual.summary()["shortage_t"]
    assert residual.summary()["shortage_t"] < 1e-6
    assert affected.payload["risk_register"][0]["physical_consequence_t"] > 0
    assert len(compare(single_year, funded_plan)) == 4
    assert demand_threshold(single_year, funded_plan)["status"] == "found"
    quantity = sum(o.volume_t for o in funded_plan.decisions.supply_orders)
    generated = generate_schedule(single_year, Plan(plan_id="generated"), [dict(source_id="A", year=2035, volume_t=quantity)])
    assert sum(o.volume_t for o in generated.decisions.supply_orders) == pytest.approx(quantity)
    assert evaluate(single_year, generated).feasible
