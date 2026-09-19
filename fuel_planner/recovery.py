"""A disclosed response using already commissioned, paid-for contract rights."""
from __future__ import annotations

import hashlib
from collections import defaultdict

from .engine import available_day, evaluate, investment_state, scenario_values, source_lead
from .models import Order, Plan, Reservation, day_index, day_label


def reserve_flexibility(case, plan: Plan) -> Plan:
    """Precommit optional capacity; fees/TOP are subsequently paid by the engine."""
    p = plan.model_copy(deep=True)
    existing = {(r.source_id, r.year): r for r in p.decisions.capacity_reservations}
    active, _, _ = investment_state(case, p)
    for year in case.years:
        if year < 2038:
            continue
        for sid in ["B", "C", "E"]:
            if sid not in case.sources or available_day(case, sid, active) > (year-case.years[0])*365:
                continue
            key = (sid, year)
            capacity = case.sources[sid]["capacity_t_per_year"]
            if key in existing:
                existing[key].annual_capacity_t = capacity
            else:
                existing[key] = Reservation(source_id=sid, year=year, annual_capacity_t=capacity)
    p.decisions.capacity_reservations = list(existing.values())
    p.description = "Диверсификация: Core/Flex/New/ISRU, ZBO; дополнительные договорные права B/C/E с 2038, оплаченные до риска"
    return p


def recovery_plan(case, plan: Plan, scenario="MANDATORY_STRESS", decision_date="2038-01-01", coordination_cost_mln=20.0, risk=None):
    if plan.adaptation:
        raise ValueError("Исходным должен быть неадаптированный план")
    first = case.years[0]
    decision = day_index(decision_date, first)
    if not 0 <= decision < len(case.years)*365 or coordination_cost_mln < 0:
        raise ValueError("Некорректные дата или стоимость ответа")
    original = evaluate(case, plan, scenario, risk)
    p = plan.model_copy(deep=True)
    p.plan_id = plan.plan_id + "-response"
    p.scenario_id = scenario
    p.description = plan.description + f"; отдельный ответ для {scenario}, решение {decision_date}"
    p.adaptation = dict(scenario_id=scenario, decision_date=decision_date, coordination_cost_mln=coordination_cost_mln,
        minimum_reaction_days=42, committed_orders=len(plan.decisions.supply_orders),
        base_plan_fingerprint=hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
        rule="Сохранить все исходные заказы, инвестиции и договоры; добавлять только в невыбранные оплаченные права, без увеличения мощности; E максимум в первые два года ответа")
    remaining = {(c["source_id"], c["year"]): c["uncalled_contract_t"] for c in original.payload["contract_ledger"]}
    reservations = {(r.source_id, r.year): r for r in plan.decisions.capacity_reservations}
    arrivals = defaultdict(list)
    for row in original.payload["source_schedule"]:
        if row["valid"] and not row["in_transit"]:
            arrivals[day_index(row["actual_date"], first)].append(row)
    active, _, _ = investment_state(case, plan)
    n = len(case.years)*365
    capacities, inflows, demands = [], [], []
    for day in range(n):
        storage = case.storage["ZBO" if day >= active["ZBO"] else "BASE"]
        capacities.append(storage["capacity_t"])
        inflows.append(sum(r["arrival_t"] for r in arrivals[day])*(1-storage["loss_rate_on_throughput"]))
        demands.append(scenario_values(case, first+day//365, scenario)[0]/365)
    # Preserve headroom for irrevocable future deliveries. Extra fuel today must
    # not force rejection of a previously committed batch tomorrow.
    ceiling = [0.0]*n
    ceiling[-1] = max(0, capacities[-1]-demands[-1])
    for day in range(n-2, -1, -1):
        ceiling[day] = max(0, min(capacities[day]-demands[day], capacities[day+1]-inflows[day+1], ceiling[day+1]+demands[day+1]-inflows[day+1]))
    inventory = 0.0
    for day in range(len(case.years)*365):
        year = first + day//365
        storage = case.storage["ZBO" if day >= active["ZBO"] else "BASE"]
        net_rate = 1-storage["loss_rate_on_throughput"]
        gross = sum(r["arrival_t"] for r in arrivals[day])
        inventory = min(storage["capacity_t"], inventory+gross*net_rate)
        demand = scenario_values(case, year, scenario)[0]/365
        # A standing reserve helps subsequent shocks; no impossible retroactive first-year fix.
        next_year = min(year+1, case.years[-1])
        target = min(ceiling[day], max(scenario_values(case, year, scenario)[0], scenario_values(case, next_year, scenario)[0])*45/365)
        needed = max(0.0, min(storage["capacity_t"], demand+target)-inventory)
        if day >= decision+42:
            for sid in sorted(case.sources, key=lambda s: (case.sources[s]["variable_cost_mln_per_t"], s)):
                if needed < 1e-8 or (risk and sid == risk.source_id):
                    continue
                source = case.sources[sid]
                lead = max(42, 0 if sid == "C" else source_lead(source, plan))
                if day < decision+lead or day-lead < available_day(case, sid, active) and sid == "D":
                    continue
                if sid == "E" and year > int(decision_date[:4])+1:
                    continue
                res = reservations.get((sid, year))
                if res is None or not res.start_day <= day%365 < res.end_day:
                    continue
                share = case.scenarios.get(scenario, {}).get("actual_delivery_share", {}).get(source["name"], {}).get(year, 1)
                if share <= 0:
                    continue
                volume = min(remaining.get((sid, year), 0), needed/(net_rate*share))
                if volume > 1e-8:
                    p.decisions.supply_orders.append(Order(source_id=sid, order_date=day_label(day-lead, first), delivery_date=day_label(day, first), volume_t=volume, contingency=True))
                    remaining[sid, year] -= volume
                    inventory += volume*net_rate*share
                    needed = max(0, min(storage["capacity_t"], demand+target)-inventory)
        inventory = max(0, inventory-demand)
    return p
