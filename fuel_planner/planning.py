from __future__ import annotations

from .data import Case
from .engine import available_day, investment_state, reserve_t, source_lead
from .models import Order, Plan, Reservation, day_label


def generate_schedule(case: Case, plan: Plan, annual_rows: list[dict]) -> Plan:
    """Spread entered gross annual volumes; first-year totals include startup stock."""
    result = plan.model_copy(deep=True)
    active, _, _ = investment_state(case, result)
    first = case.years[0]
    orders, reservations = [], []
    seen = set()
    initial_net_remaining = case.demand[first]["base_total_t"] * max(45.0, plan.decisions.inventory_policy.get("target_days", 45))/365
    for row in sorted(annual_rows, key=lambda r: (int(r["year"]), str(r["source_id"]))):
        sid, year, volume = str(row["source_id"]), int(row["year"]), float(row["volume_t"])
        if (sid, year) in seen or sid not in case.sources or year not in case.years or volume < 0:
            raise ValueError("Некорректная строка годового заказа или повтор источника и года")
        seen.add((sid, year))
        if volume == 0:
            continue
        source = case.sources[sid]
        year_start = (year-first)*365
        commission = available_day(case, sid, active)
        lead = 0 if sid == "C" else source_lead(source, result)
        delivery_start = max(year_start, commission + (lead if sid == "D" else 0))
        contract_start = max(year_start, commission)
        end = year_start + 365
        if delivery_start >= end:
            raise ValueError(f"{sid} недоступен в {year}")
        reservations.append(Reservation(source_id=sid, year=year, annual_capacity_t=volume*365/(end-contract_start), start_day=contract_start-year_start))
        if year == first and delivery_start == 0 and initial_net_remaining > 0:
            loss = case.storage["ZBO" if active["ZBO"] <= 0 else "BASE"]["loss_rate_on_throughput"]
            startup = min(volume, initial_net_remaining/(1-loss))
            orders.append(Order(source_id=sid, order_date=day_label(-lead, first), delivery_date=day_label(0, first), volume_t=startup, startup=True))
            volume -= startup
            initial_net_remaining -= startup*(1-loss)
        for day in range(delivery_start, end):
            if volume > 0:
                orders.append(Order(source_id=sid, order_date=day_label(day-lead, first), delivery_date=day_label(day, first), volume_t=volume/(end-delivery_start)))
    result.decisions.supply_orders = orders
    result.decisions.capacity_reservations = reservations
    return result
