"""Traceability tables and independent reconciliation of evaluated results."""
from __future__ import annotations

from math import fsum

from .models import day_index, day_label


def enrich(case, plan, result):
    from .engine import available_day, investment_state, threshold
    p = result.payload
    ledger, controls = [], []
    active, _, _ = investment_state(case, plan)

    def check(rule, year, actual, limit, unit, operator="<=", source="", severity="hard"):
        passed = actual <= limit+1e-6 if operator == "<=" else actual+1e-6 >= limit
        ledger.append(dict(rule_id=rule, year=year, source_id=source, actual=actual, limit=limit,
                           unit=unit, operator=operator, passed=passed, severity=severity,
                           excess=0.0 if passed else abs(actual-limit)))

    for y in p["yearly_balance"]:
        year = y["year"]
        daily = [d for d in p["inventory_trace"] if d["year"] == year]
        mass = fsum([y["opening_inventory_t"], y["delivered_t"], -y["losses_t"], -y["served_t"], -y["closing_inventory_t"]])
        previous = next((row["closing_inventory_t"] for row in p["yearly_balance"] if row["year"] == year-1), 0.0)
        control = dict(year=year, material_balance_residual_t=mass, carry_residual_t=y["opening_inventory_t"]-previous,
            max_daily_balance_residual_t=max(abs(fsum([d["opening_inventory_t"], d["delivered_t"], -d["losses_t"], -d["served_t"], -d["closing_inventory_t"]])) for d in daily))
        for metric, cid, default in [("total_service_level", "BASE_TOTAL_SERVICE", .97), ("critical_service_level", "BASE_CRITICAL_SERVICE", .99)]:
            check(cid, year, y[metric], threshold(case, cid, default), "share", ">=", severity="hard" if p["scenario_id"] == "BASE" else "service")
        check("RESERVE_45D", year, y["reserve_opening_t"], y["reserve_required_t"], "t", ">=")
        check("NONNEGATIVE_INVENTORY", year, min(d["closing_inventory_t"] for d in daily), 0, "t", ">=")
        peak_excess = max([d["closing_inventory_t"]+d["served_t"]-d["capacity_t"] for d in daily] + [v["excess"] for v in p["constraint_checks"] if v["rule_id"] == "STORAGE_OVERFLOW" and v["year"] == year])
        check("STORAGE_CAPACITY", year, max(0, peak_excess), 0, "t")
        check("MATERIAL_BALANCE", year, abs(mass), 1e-6, "t")
        check("CRITICAL_NESTED", year, y["critical_served_t"], y["served_t"], "t")
        if year <= 2040:
            cutoff = 2037 if year <= 2037 else 2040
            total_capex = fsum(f["capex_mln"] for f in p["financial_breakdown"] if f["year"] <= year)
            check(f"CAPEX_{cutoff}", year, total_capex, threshold(case, f"CAPEX_{cutoff}", 1800 if cutoff == 2037 else 2800), "mln_units")
        if p["scenario_id"] == "MANDATORY_STRESS" and 2038 <= year <= 2040:
            check("STRESS_LOSS_LIMIT", year, y["loss_ratio"], threshold(case, "STRESS_LOSS_LIMIT", .02), "share")
        for cid in ["LEAD_TIME", "SOURCE_UNAVAILABLE", "ISRU_FINANCING_DEADLINE", "ZBO_EARLIEST_YEAR", "EMERGENCY_BASE_STREAK", "NO_ACTIVE_CONTRACT", "RESERVATION_BEFORE_COMMISSIONING"]:
            check(cid, year, sum(v["year"] == year and v["rule_id"] == cid for v in p["constraint_checks"]), 0, "violations")
        for row in [c for c in p["contract_ledger"] if c["year"] == year]:
            sid = row["source_id"]
            check("CAPACITY_EXCEEDED", year, row["reserved_capacity_t"], row["source_capacity_t"], "t/year", source=sid)
            check("ORDER_EXCEEDS_CONTRACT", year, row["ordered_t"], min(row["contracted_volume_t"], row["available_capacity_t"]), "t", source=sid)
            schedule = [s for s in p["source_schedule"] if s["source_id"] == sid and int(s["actual_date"][:4]) == year]
            row["actually_arrived_t"] = fsum(s["arrival_t"] for s in schedule if not s["in_transit"])
            row["accepted_t"] = fsum(s["delivered_t"] for s in schedule)
            check("ARRIVAL_CAPACITY_EXCEEDED", year, row["actually_arrived_t"], row["available_capacity_t"], "t", source=sid)
        # Independent monetary reconstruction: base order payment plus unused TOP.
        recomputed_procurement, recomputed_reservation = 0.0, 0.0
        for c in [c for c in p["contract_ledger"] if c["year"] == year]:
            orders = fsum(o.volume_t for o in plan.decisions.supply_orders if o.source_id == c["source_id"] and int(o.delivery_date[:4]) == year)
            contract = next((r for r in plan.decisions.capacity_reservations if r.year == year and r.source_id == c["source_id"]), None)
            period_volume = contract.annual_capacity_t*(contract.end_day-contract.start_day)/365 if contract else 0
            source = case.sources[c["source_id"]]
            unused_minimum = max(0, period_volume*source["take_or_pay_share"]-orders)
            recomputed_procurement += (orders+unused_minimum)*c["price_mln_per_t"]
            recomputed_reservation += period_volume*source["reservation_rate_mln_per_t_year_capacity"]
        money = next(f for f in p["financial_breakdown"] if f["year"] == year)
        control["procurement_residual_mln"] = recomputed_procurement-money["procurement_mln"]
        control["reservation_residual_mln"] = recomputed_reservation-money["reservation_mln"]
        control["cost_partition_residual_mln"] = money["total_cost_mln"]-fsum(money[k] for k in ["fuel_purchasing_mln", "take_or_pay_mln", "reservation_mln", "holding_mln", "fixed_opex_mln", "capex_mln", "mitigation_mln"])
        control["passed"] = all(abs(v) <= 1e-6 for k, v in control.items() if k != "year")
        controls.append(control)
    p["constraint_ledger"], p["independent_controls"] = ledger, controls
    p["roadmap"] = roadmap(case, plan, active)
    p["stakeholders"] = stakeholder_table(result)
    p["units"].update(share="0..1", capacity="t/year", unit_price="mln_units/t", lead="day")
    p["assumptions_reference"]["cost_comparison"] = "Undiscounted sum in constant 2035 prices; no revenue, profit or inferred mission-loss valuation"
    p["assumptions_reference"]["stakeholder_allocation"] = "Operator bears non-CAPEX cash costs; financier bears CAPEX; consumers' monetary loss unknown; launch share of Earth procurement = 30% TEAM_ASSUMPTION, not an added charge"


def roadmap(case, plan, active):
    rows = []
    for i in plan.decisions.investments:
        rows.append(dict(date=i.decision_date, milestone=f"Финансирование {i.investment_id}", dependency="Одобрение финансирующей стороны", owner="financier", value_mln=case.investments[i.investment_id]["exercise_cost_mln"]))
        if i.investment_id == "EARTH_NEW":
            rows.append(dict(date=i.option_date or i.decision_date, milestone="Опцион Earth-New", dependency="До реализации опциона", owner="financier", value_mln=90))
        if active[i.investment_id] < 10**8:
            rows.append(dict(date=day_label(active[i.investment_id], case.years[0]), milestone=f"Ввод {i.investment_id}", dependency=f"Оплата {i.decision_date} и срок подготовки", owner="operator", value_mln=0))
    for r in plan.decisions.capacity_reservations:
        orders = [o for o in plan.decisions.supply_orders if o.source_id == r.source_id and int(o.delivery_date[:4]) == r.year]
        rows.append(dict(date=min((o.order_date for o in orders), default=f"{r.year}-01-01"), milestone=f"Договор {r.source_id}, {r.year}: {r.annual_capacity_t:g} т/год",
                         dependency="Зарезервировать до первого заказа; поставка после ввода", owner="operator / supplier / launch_provider", value_mln=None))
    return sorted(rows, key=lambda r: (r["date"], r["milestone"]))


def stakeholder_table(result):
    rows = []
    for y in result.payload["yearly_balance"]:
        year = y["year"]
        f = next(f for f in result.payload["financial_breakdown"] if f["year"] == year)
        contracts = [c for c in result.payload["contract_ledger"] if c["year"] == year]
        launch_receipts = .30*sum(c["fuel_purchasing_mln"]+c["take_or_pay_mln"] for c in contracts if c["source_id"] != "D")
        critical_shortage = max(0, y["critical_demand_t"]-y["critical_served_t"])
        noncritical = y["demand_t"]-y["critical_demand_t"]
        other_served = y["served_t"]-y["critical_served_t"]
        launch_schedule = [s for s in result.payload["source_schedule"] if s["year"] == year and s["source_id"] != "D"]
        launch_ordered = sum(s["ordered_t"] for s in launch_schedule)
        on_time = sum(s["delivered_t"] for s in launch_schedule if s["valid"] and s["actual_date"] <= s["scheduled_date"])
        specs = [
            ("operator", "Оператор", "Обслуживание и ликвидность", "Исполнить договоры, баланс и резерв", f["total_cost_mln"]-f["capex_mln"], 0, y["total_service_level"], y["shortage_t"], "Дефицит, переполнение, затраты ответа"),
            ("critical_consumers", "Критические потребители", "Непрерывность критических миссий", "Предоставлять график; приоритет при дефиците", 0, 0, y["critical_service_level"], critical_shortage, "Критический недоотпуск; денежная оценка отсутствует"),
            ("commercial_consumers", "Коммерческие потребители", "Доступность остаточного топлива", "Признать приоритет критических потребителей", 0, 0, other_served/noncritical if noncritical else 1, max(0, noncritical-other_served), "Первыми несут сокращение сервиса; выручка не задана"),
            ("fuel_suppliers", "Поставщики топлива", "Оплата заказов и TOP", "Производство в рамках мощности и договора", 0, f["procurement_mln"]+f["reservation_mln"]-launch_receipts, 1.0 if not sum(c["ordered_t"] for c in contracts) else min(1, sum(c["actually_arrived_t"] for c in contracts)/sum(c["ordered_t"] for c in contracts)), 0, "Недопоставка; TOP защищает платеж, но не физический ресурс"),
            ("launch_providers", "Поставщики запусков", "Оплаченный график запусков", "Соблюдать lead time; транспорт включен в цену топлива", 0, launch_receipts, min(1, on_time/launch_ordered) if launch_ordered else 1, max(0, launch_ordered-on_time), "Задержки и отклонение топлива; доля 30% — допущение учета"),
            ("financier", "Финансирующая сторона", "CAPEX и финансовая потребность", "Финансировать до ввода; не обещать прибыль без выручки", f["capex_mln"], 0, f["capex_mln"], 0, "Невозвратные инвестиции и дополнительная ликвидность"),
        ]
        for sid, name, interest, obligation, cost, receipt, kpi, impact, risk in specs:
            rows.append(dict(year=year, stakeholder_id=sid, stakeholder=name, interests=interest, obligations=obligation,
                             allocated_cash_cost_mln=cost, attributed_receipts_mln=receipt, kpi_value=kpi,
                             kpi_name={"operator":"total_service", "critical_consumers":"critical_service", "commercial_consumers":"noncritical_service", "fuel_suppliers":"annual_arrival_to_order_ratio", "launch_providers":"on_time_accepted_to_order_ratio", "financier":"annual_capex"}[sid],
                             kpi_unit="mln_units CAPEX" if sid == "financier" else "share", affected_fuel_t=impact, risks_borne=risk,
                             monetary_mission_loss="not specified", weights_used=False))
    return rows
