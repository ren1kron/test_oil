from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from . import __version__
from .data import Case
from .models import Plan, Risk, day_index, day_label, lead_days

TOL = 1e-6


def payable_volume(order: float, reserved_period: float, top: float) -> float:
    return max(order, top * reserved_period)


def reservation_payment(rate: float, reserved: float, fraction: float = 1) -> float:
    return rate * reserved * fraction


def reserve_t(demand: float) -> float:
    return demand * 45 / 365


def balance(opening: float, delivered: float, losses: float, served: float) -> float:
    return opening + delivered - losses - served


def serve(available: float, total: float, critical: float):
    critical_served = min(max(available, 0), critical)
    other_served = min(max(0, available - critical_served), total - critical)
    served = critical_served + other_served
    return served, critical_served, max(0, total - served), max(0, available - served)


def capacity_excess(actual: float, maximum: float) -> float:
    return max(0, actual - maximum)


def violation(rule, period, actual, limit, source=None, severity="hard", message=""):
    return dict(rule_id=rule, period=str(period), source_id=source or "", actual=actual,
                limit=limit, excess=abs(actual - limit), severity=severity, message=message)


def threshold(case: Case, key: str, default: float) -> float:
    return next((r["value"] for r in case.constraints if r["constraint_id"] == key), default)


def source_lead(source: dict, plan: Plan) -> int:
    field = "lead_time_max_value" if plan.assumptions.lead_time_policy == "upper" else "lead_time_min_value"
    return lead_days(source[field], source["lead_time_unit"])


def investment_state(case: Case, plan: Plan):
    first = case.years[0]
    active = {"EARTH_NEW": 10**9, "LUNAR_ISRU": 10**9, "ZBO": 10**9}
    payments = defaultdict(float)
    errors = []
    seen = set()
    for inv in plan.decisions.investments:
        if inv.investment_id in seen:
            raise ValueError(f"Повторная инвестиция {inv.investment_id}")
        seen.add(inv.investment_id)
        d = day_index(inv.decision_date, first)
        year = first + d // 365
        if year not in case.years:
            raise ValueError("Инвестиции должны находиться в расчетном горизонте")
        cfg = case.investments[inv.investment_id]
        if inv.investment_id == "EARTH_NEW":
            option_date = inv.option_date or inv.decision_date
            option_day = day_index(option_date, first)
            if not 0 <= option_day <= d:
                raise ValueError("Опцион оплачивается в горизонте, не позже реализации")
            payments[first + option_day // 365] += cfg["option_fee_mln"]
            payments[year] += cfg["exercise_cost_mln"]
            active[inv.investment_id] = d + source_lead(case.sources["C"], plan)
        elif inv.investment_id == "LUNAR_ISRU":
            payments[year] += cfg["total_capex_mln"]
            if year >= 2038:
                errors.append(violation("ISRU_FINANCING_DEADLINE", year, year, 2037))
            else:
                active[inv.investment_id] = (2038 - first) * 365
        else:
            payments[year] += cfg["total_capex_mln"]
            earliest = int(case.storage["ZBO"]["available_from_year"])
            if year < earliest:
                errors.append(violation("ZBO_EARLIEST_YEAR", year, year, earliest))
            else:
                active[inv.investment_id] = d
    return active, payments, errors


def available_day(case: Case, sid: str, active: dict) -> int:
    if sid == "C":
        return active["EARTH_NEW"]
    fixed = (int(case.sources[sid]["available_from_year"] or case.years[0]) - case.years[0]) * 365
    return max(fixed, active["LUNAR_ISRU"]) if sid == "D" else fixed


def scenario_values(case: Case, year: int, scenario: str, demand_scale=1.0):
    row = case.demand[year]
    if scenario in {"LOW_DEMAND", "HIGH_DEMAND"}:
        total = row["low_total_t" if scenario == "LOW_DEMAND" else "high_total_t"]
        critical = total * row["base_critical_t"] / row["base_total_t"] if row["base_total_t"] else 0
    else:
        cfg = case.scenarios.get(scenario, case.scenarios["BASE"])
        total_map = cfg.get("demand_multiplier", {})
        critical_map = cfg.get("critical_demand_multiplier", {})
        total = row["base_total_t"] * total_map.get(year, total_map.get("default", 1))
        critical = row["base_critical_t"] * critical_map.get(year, critical_map.get("default", 1))
    return total * demand_scale, critical * demand_scale


def price_multiplier(case, scenario, source, year, risk=None):
    cfg = case.scenarios.get(scenario, {})
    prices = cfg.get("variable_price_multiplier", {})
    value = prices.get(source["name"], {}).get(year, prices.get("default", 1))
    if risk and source["source_id"] == risk.source_id and risk.start_year <= year <= risk.end_year:
        value *= risk.price_multiplier
    return value


@dataclass
class Result:
    payload: dict[str, Any]

    @property
    def feasible(self):
        return not any(v["severity"] == "hard" for v in self.payload["constraint_checks"])

    @property
    def total_cost(self):
        return sum(r["total_cost_mln"] for r in self.payload["financial_breakdown"])

    def summary(self):
        years = self.payload["yearly_balance"]
        return dict(plan_id=self.payload["plan_id"], scenario_id=self.payload["scenario_id"],
                    feasible=self.feasible, total_cost_mln=self.total_cost,
                    shortage_t=sum(r["shortage_t"] for r in years),
                    min_total_service=min(r["total_service_level"] for r in years),
                    min_critical_service=min(r["critical_service_level"] for r in years),
                    violations=len(self.payload["constraint_checks"]))


def evaluate(case: Case, plan: Plan, scenario: str = "BASE", risk: Risk | None = None,
             demand_scale: float = 1.0) -> Result:
    import hashlib
    import math
    if not math.isfinite(demand_scale) or demand_scale < 0:
        raise ValueError("Множитель спроса должен быть конечным и неотрицательным")
    if scenario not in case.scenarios and scenario not in {"LOW_DEMAND", "HIGH_DEMAND"} and not (risk and scenario == risk.risk_id):
        raise ValueError(f"Неизвестный сценарий {scenario}")
    if risk and (scenario != risk.risk_id or risk.source_id not in case.sources):
        raise ValueError("Риск должен иметь собственный сценарий и известный источник")
    first, n = case.years[0], len(case.years) * 365
    active, capex, checks = investment_state(case, plan)
    reservations = {}
    for res in plan.decisions.capacity_reservations:
        if res.source_id not in case.sources or res.year not in case.years:
            raise ValueError("Неизвестный источник или год резервирования")
        key = (res.source_id, res.year)
        if key in reservations:
            raise ValueError("Допускается один договор на источник и год")
        reservations[key] = res
        source = case.sources[res.source_id]
        if res.annual_capacity_t > source["capacity_t_per_year"] + TOL:
            checks.append(violation("CAPACITY_EXCEEDED", res.year, res.annual_capacity_t, source["capacity_t_per_year"], res.source_id))
        if res.annual_capacity_t > TOL and (res.year-first)*365 + res.start_day < available_day(case, res.source_id, active):
            checks.append(violation("RESERVATION_BEFORE_COMMISSIONING", res.year, (res.year-first)*365 + res.start_day, available_day(case, res.source_id, active), res.source_id))

    arrivals = defaultdict(list)
    schedule = []
    annual_orders = defaultdict(float)
    emergency_years = set()
    for i, order in enumerate(plan.decisions.supply_orders):
        if order.source_id not in case.sources:
            raise ValueError(f"Неизвестный источник {order.source_id}")
        sid = order.source_id
        source = case.sources[sid]
        scheduled = day_index(order.delivery_date, first)
        if not 0 <= scheduled < n:
            raise ValueError("Плановая поставка вне расчетного горизонта")
        year = first + scheduled // 365
        if order.startup and (scheduled != 0 or not plan.assumptions.opening_stock_funding.strip()):
            raise ValueError("Стартовая партия требует дату начала горизонта и описание финансирования")
        if order.contingency and not risk:
            raise ValueError("Контингентная активация разрешена только в отдельном риск-сценарии")
        annual_orders[sid, year] += order.volume_t
        if sid == "E" and order.volume_t > TOL and not order.contingency:
            emergency_years.add(year)
        delay, share = 0, 1.0
        cfg = case.scenarios.get(scenario, {})
        shares = cfg.get("actual_delivery_share", {})
        share = shares.get(source["name"], {}).get(year, shares.get("default", 1.0))
        if risk and sid == risk.source_id and risk.start_year <= year <= risk.end_year:
            delay, share = risk.delay_days, risk.delivery_share
        arrival = scheduled + delay
        valid = True
        lead = 0 if sid == "C" else source_lead(source, plan)
        ordered = day_index(order.order_date, first)
        if scheduled - ordered < lead:
            checks.append(violation("LEAD_TIME", order.delivery_date, scheduled-ordered, lead, sid))
            valid = False
        start = available_day(case, sid, active)
        if scheduled < start or (sid == "D" and ordered < start):
            checks.append(violation("SOURCE_UNAVAILABLE", order.delivery_date, scheduled, start + (lead if sid == "D" else 0), sid))
            valid = False
        res = reservations.get((sid, year))
        if not res or not res.start_day <= scheduled % 365 < res.end_day:
            checks.append(violation("NO_ACTIVE_CONTRACT", order.delivery_date, order.volume_t, 0, sid))
            valid = False
        record = dict(order_id=i, source_id=sid, order_date=order.order_date, scheduled_date=order.delivery_date,
                      actual_date=day_label(arrival, first), ordered_t=order.volume_t, arrival_t=order.volume_t*share if valid else 0,
                      delivered_t=0.0, rejected_t=0.0, startup=order.startup, in_transit=arrival >= n,
                      valid=valid, year=year)
        schedule.append(record)
        if valid and arrival < n:
            arrivals[arrival].append(record)

    financial = {}
    for year in case.years:
        money = dict(year=year, procurement_mln=0.0, reservation_mln=0.0, holding_mln=0.0, fixed_opex_mln=0.0, capex_mln=capex[year])
        for sid, source in case.sources.items():
            order = annual_orders[sid, year]
            res = reservations.get((sid, year))
            fraction = (res.end_day-res.start_day)/365 if res else 0.0
            reserved = res.annual_capacity_t if res else 0.0
            maximum = min(reserved, source["capacity_t_per_year"]) * fraction
            if order > maximum + TOL:
                checks.append(violation("ORDER_EXCEEDS_CONTRACT", year, order, maximum, sid))
            money["procurement_mln"] += payable_volume(order, reserved*fraction, source["take_or_pay_share"]) * source["variable_cost_mln_per_t"] * price_multiplier(case, scenario, source, year, risk)
            money["reservation_mln"] += reservation_payment(source["reservation_rate_mln_per_t_year_capacity"], reserved, fraction)
        financial[year] = money

    inventory = 0.0
    trace = []
    yearly = {}
    for year in case.years:
        total, critical = scenario_values(case, year, scenario, demand_scale)
        yearly[year] = dict(year=year, demand_t=total, critical_demand_t=critical, opening_inventory_t=0.0,
                            delivered_t=0.0, losses_t=0.0, served_t=0.0, critical_served_t=0.0,
                            shortage_t=0.0, rejected_t=0.0, reserve_required_t=reserve_t(total))
    for day in range(n):
        year = first + day // 365
        totals = yearly[year]
        storage = case.storage["ZBO" if day >= active["ZBO"] else "BASE"]
        opening = inventory
        if day % 365 == 0:
            totals["opening_inventory_t"] = opening
        daily = dict(day=day, date=day_label(day, first), year=year, opening_inventory_t=opening,
                     delivered_t=0.0, losses_t=0.0, rejected_t=0.0, capacity_t=storage["capacity_t"],
                     reserve_required_t=totals["reserve_required_t"])

        def receive(record):
            nonlocal inventory
            gross = record["arrival_t"]
            rate = storage["loss_rate_on_throughput"]
            excess = max(0.0, inventory + gross*(1-rate) - storage["capacity_t"])
            rejected = excess / (1-rate)
            accepted = max(0.0, gross-rejected)
            if excess > TOL:
                checks.append(violation("STORAGE_OVERFLOW", daily["date"], inventory + gross*(1-rate), storage["capacity_t"], record["source_id"], message="Избыток физически отклонен; закупка остается оплаченной"))
            record["delivered_t"], record["rejected_t"] = accepted, rejected
            daily["delivered_t"] += accepted
            daily["losses_t"] += accepted * rate
            daily["rejected_t"] += rejected
            inventory += accepted*(1-rate)

        for record in arrivals[day]:
            if record["startup"]:
                receive(record)
        if day % 365 == 0:
            daily["reserve_check_t"] = inventory
            totals["reserve_opening_t"] = inventory
            if inventory + TOL < totals["reserve_required_t"]:
                checks.append(violation("RESERVE_45D", year, inventory, totals["reserve_required_t"]))
        for record in arrivals[day]:
            if not record["startup"]:
                receive(record)
        before_service = inventory
        served, critical_served, shortage, inventory = serve(inventory, totals["demand_t"]/365, totals["critical_demand_t"]/365)
        daily.update(served_t=served, critical_served_t=critical_served, shortage_t=shortage, closing_inventory_t=inventory)
        daily["average_inventory_t"] = (before_service + inventory)/2
        for key in ["delivered_t", "losses_t", "served_t", "critical_served_t", "shortage_t", "rejected_t"]:
            totals[key] += daily[key]
        totals["closing_inventory_t"] = inventory
        financial[year]["holding_mln"] += daily["average_inventory_t"] * storage["holding_cost_mln_per_t_year"]/365
        for inv in ["ZBO", "LUNAR_ISRU"]:
            if day >= active[inv]:
                financial[year]["fixed_opex_mln"] += case.investments[inv]["fixed_opex_mln_per_year"]/365
        trace.append(daily)
    streak = 0
    for year in case.years:
        totals = yearly[year]
        totals["total_service_level"] = totals["served_t"]/totals["demand_t"] if totals["demand_t"] else 1.0
        totals["critical_service_level"] = totals["critical_served_t"]/totals["critical_demand_t"] if totals["critical_demand_t"] else 1.0
        totals["loss_ratio"] = totals["losses_t"]/totals["delivered_t"] if totals["delivered_t"] else 0.0
        for metric, key, default in [("total_service_level", "BASE_TOTAL_SERVICE", .97), ("critical_service_level", "BASE_CRITICAL_SERVICE", .99)]:
            limit = threshold(case, key, default)
            if totals[metric] + TOL < limit:
                checks.append(violation(key if scenario == "BASE" else f"SERVICE_REFERENCE_{metric.upper()}", year, totals[metric], limit, severity="hard" if scenario == "BASE" else "service"))
        if scenario == "MANDATORY_STRESS" and 2038 <= year <= 2040 and totals["loss_ratio"] > threshold(case, "STRESS_LOSS_LIMIT", .02) + TOL:
            checks.append(violation("STRESS_LOSS_LIMIT", year, totals["loss_ratio"], threshold(case, "STRESS_LOSS_LIMIT", .02)))
        streak = streak + 1 if year in emergency_years else 0
        if streak > threshold(case, "EMERGENCY_BASE_STREAK", 2):
            checks.append(violation("EMERGENCY_BASE_STREAK", year, streak, threshold(case, "EMERGENCY_BASE_STREAK", 2), "E"))
        financial[year]["total_cost_mln"] = sum(v for k, v in financial[year].items() if k.endswith("_mln"))
    for year, cid in [(2037, "CAPEX_2037"), (2040, "CAPEX_2040")]:
        if year in case.years:
            cumulative = sum(value for y, value in capex.items() if y <= year)
            limit = threshold(case, cid, 1800 if year == 2037 else 2800)
            if cumulative > limit + TOL:
                checks.append(violation(cid, year, cumulative, limit))
    payload = dict(schema_version=1, software_version=__version__, scenario_id=scenario, plan_id=plan.plan_id,
                   plan_fingerprint=hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
                   scenario_definition=risk.model_dump() if risk else case.scenarios.get(scenario, {"demand_column": "low_total_t" if scenario == "LOW_DEMAND" else "high_total_t"}),
                   input_fingerprint=case.fingerprint, units={"fuel": "t", "money": "mln_units_2035", "time": "365_day_year"},
                   assumptions_reference={**plan.assumptions.model_dump(), "case_extensions": case.assumptions,
                                          "earth_new": "lead time is commissioning; subsequent deliveries scheduled under annual capacity",
                                          "payment": "annual delivery-year orders paid even under delivery disruption",
                                          "demand_scale": demand_scale},
                   yearly_balance=list(yearly.values()), source_schedule=schedule, inventory_trace=trace,
                   financial_breakdown=list(financial.values()), constraint_checks=checks, risk_register=[])
    return Result(payload)
