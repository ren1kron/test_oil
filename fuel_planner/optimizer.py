from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .data import Case, case_snapshot, load_case
from .engine import evaluate, reserve_t, source_lead, threshold
from .models import Decisions, Investment, Order, Plan, Reservation, day_label


@dataclass
class Settings:
    time_limit: float = 120.0
    relative_gap: float = 0.001
    disabled_sources: list[str] = field(default_factory=list)
    # auto / never / a decision year; the ISRU financing year is 2037.
    investments: dict[str, str | int] = field(default_factory=dict)
    plan_id: str = "optimized"


@dataclass
class Optimization:
    status: str
    message: str
    elapsed_seconds: float
    plan: Plan | None = None
    objective_mln: float | None = None
    lower_bound_mln: float | None = None
    relative_gap: float | None = None
    validated: bool = False

    def metadata(self):
        return {k: v for k, v in self.__dict__.items() if k != "plan"}


class Matrix:
    def __init__(self):
        self.cost, self.lower, self.upper, self.integer = [], [], [], []
        self.rows, self.cols, self.values, self.lo, self.hi = [], [], [], [], []

    def var(self, cost=0.0, lower=0.0, upper=np.inf, integer=0):
        i = len(self.cost)
        self.cost.append(cost)
        self.lower.append(lower)
        self.upper.append(upper)
        self.integer.append(integer)
        return i

    def constraint(self, pairs, lower=-np.inf, upper=np.inf):
        row = len(self.lo)
        for col, value in pairs:
            self.rows.append(row)
            self.cols.append(col)
            self.values.append(value)
        self.lo.append(lower)
        self.hi.append(upper)

    def solve(self, settings):
        a = coo_matrix((self.values, (self.rows, self.cols)), shape=(len(self.lo), len(self.cost))).tocsc()
        return milp(np.array(self.cost), integrality=np.array(self.integer),
                    bounds=Bounds(self.lower, self.upper), constraints=LinearConstraint(a, self.lo, self.hi),
                    options={"time_limit": settings.time_limit, "mip_rel_gap": settings.relative_gap})


def optimize(case: Case, settings: Settings | None = None) -> Optimization:
    settings = settings or Settings()
    if not 0 < settings.time_limit <= 3600 or not 0 <= settings.relative_gap <= 1:
        raise ValueError("Некорректные ограничения решателя")
    if set(settings.disabled_sources) - case.sources.keys():
        raise ValueError("Неизвестный отключенный поставщик")
    if set(settings.investments) - {"EARTH_NEW", "ZBO", "LUNAR_ISRU"}:
        raise ValueError("Неизвестная инвестиция")
    start = monotonic()
    first, last = case.years[0], case.years[-1]
    n = len(case.years)*365
    template = Plan(plan_id=settings.plan_id)
    base, zbo = case.storage["BASE"], case.storage["ZBO"]
    if base["holding_cost_mln_per_t_year"] != zbo["holding_cost_mln_per_t_year"]:
        raise ValueError("Оптимизатор поддерживает одинаковый holding rate режимов; используйте симулятор для иных данных")
    model = Matrix()
    choices = {}
    choices_by_year = {year: [] for year in case.years}
    for kind in ["EARTH_NEW", "ZBO", "LUNAR_ISRU"]:
        years = ([2037] if first <= 2037 <= last and "D" in case.sources else []) if kind == "LUNAR_ISRU" else [y for y in case.years if (kind != "ZBO" or y >= int(zbo["available_from_year"])) and (kind != "EARTH_NEW" or ("C" in case.sources and y + 2 <= last))]
        selected = settings.investments.get(kind, "auto")
        if selected not in {"auto", "never"} and selected not in years:
            raise ValueError(f"Недопустимый год {kind}: {selected}")
        choices[kind] = {}
        for year in years:
            operating_years = max(0, last-max(2038, first)+1) if kind == "LUNAR_ISRU" else last-year+1
            fixed = case.investments[kind]["fixed_opex_mln_per_year"] * operating_years
            idx = model.var(cost=case.investments[kind]["total_capex_mln"] + fixed,
                            lower=1 if selected == year else 0,
                            upper=0 if selected == "never" or (isinstance(selected, int) and selected != year) else 1, integer=1)
            choices[kind][year] = idx
            choices_by_year[year].append((idx, case.investments[kind]["total_capex_mln"]))
        model.constraint([(v, 1) for v in choices[kind].values()], upper=1)
    for end, cid, fallback in [(2037, "CAPEX_2037", 1800), (2040, "CAPEX_2040", 2800)]:
        if end in case.years:
            model.constraint([p for y, pairs in choices_by_year.items() if y <= end for p in pairs], upper=threshold(case, cid, fallback))

    def active(kind, day):
        if kind == "LUNAR_ISRU":
            return list(choices[kind].values()) if day >= (2038-first)*365 else []
        delay = source_lead(case.sources["C"], template) if kind == "EARTH_NEW" else 0
        return [idx for year, idx in choices[kind].items() if (year-first)*365 + delay <= day]

    reserve_vars, paid_vars, emergency = {}, {}, {}
    for year in case.years:
        if "E" in case.sources:
            emergency[year] = model.var(upper=1, integer=1)
        for sid, source in case.sources.items():
            capacity = source["capacity_t_per_year"]
            enabled = sid not in settings.disabled_sources
            if source["available_from_year"] is not None and year < source["available_from_year"]:
                enabled = False
            r = model.var(source["reservation_rate_mln_per_t_year_capacity"], upper=capacity if enabled else 0)
            p = model.var(source["variable_cost_mln_per_t"])
            reserve_vars[sid, year], paid_vars[sid, year] = r, p
            model.constraint([(p, 1), (r, -source["take_or_pay_share"])], lower=0)
            if sid in {"C", "D"}:
                kind = "EARTH_NEW" if sid == "C" else "LUNAR_ISRU"
                model.constraint([(r, 1)] + [(i, -capacity) for i in active(kind, (year-first)*365)], upper=0)
            if sid == "E":
                model.constraint([(r, 1), (emergency[year], -capacity)], upper=0)
    for offset in range(len(case.years)-2):
        if emergency:
            model.constraint([(emergency[y], 1) for y in case.years[offset:offset+3]], upper=2)

    flows = {}
    annual_flows = {(sid, y): [] for sid in case.sources for y in case.years}
    inventory = []
    holding_rate = base["holding_cost_mln_per_t_year"]/365
    constant = 0.0
    max_gross = max(base["capacity_t"], zbo["capacity_t"])/(1-max(base["loss_rate_on_throughput"], zbo["loss_rate_on_throughput"]))
    for day in range(n):
        year = first + day//365
        demand = case.demand[year]["base_total_t"]/365
        stock = model.var(holding_rate, upper=max(base["capacity_t"], zbo["capacity_t"]))
        inventory.append(stock)
        constant += demand*holding_rate/2
        active_zbo = active("ZBO", day)
        # Peak inventory is closing stock plus the demand consumed during the day.
        model.constraint([(stock, 1)] + [(i, -(zbo["capacity_t"]-base["capacity_t"])) for i in active_zbo], upper=base["capacity_t"]-demand)
        net_terms, by_mode = [], [[], []]
        for sid, source in case.sources.items():
            for mode, loss in enumerate([base["loss_rate_on_throughput"], zbo["loss_rate_on_throughput"]]):
                allowed = sid not in settings.disabled_sources
                if sid == "D" and day < (2038-first)*365 + source_lead(source, template):
                    allowed = False
                if source["available_from_year"] is not None and day < (int(source["available_from_year"])-first)*365:
                    allowed = False
                flow = model.var(upper=max_gross if allowed else 0)
                flows[sid, day, mode] = flow
                annual_flows[sid, year].append((flow, 1))
                net_terms.append((flow, 1-loss))
                by_mode[mode].append((flow, 1))
        model.constraint(by_mode[0] + [(i, max_gross) for i in active_zbo], upper=max_gross)
        model.constraint(by_mode[1] + [(i, -max_gross) for i in active_zbo], upper=0)
        terms = [(stock, 1)] + [(i, -coef) for i, coef in net_terms]
        if day:
            terms.append((inventory[day-1], -1))
        model.constraint(terms, lower=-demand, upper=-demand)
        if day % 365 == 0:
            reserve = reserve_t(case.demand[year]["base_total_t"])
            model.constraint(net_terms if day == 0 else [(inventory[day-1], 1)], lower=reserve)
    for key, entries in annual_flows.items():
        model.constraint(entries + [(reserve_vars[key], -1)], upper=0)
        model.constraint(entries + [(paid_vars[key], -1)], upper=0)
    solved = model.solve(settings)
    statuses = {0: "optimal_within_gap", 1: "time_limit", 2: "infeasible", 3: "unbounded", 4: "solver_error"}
    result = Optimization(statuses.get(solved.status, "solver_error"), str(solved.message), monotonic()-start)
    if getattr(solved, "mip_dual_bound", None) is not None and np.isfinite(solved.mip_dual_bound):
        result.lower_bound_mln = float(solved.mip_dual_bound + constant)
    if getattr(solved, "mip_gap", None) is not None and np.isfinite(solved.mip_gap):
        result.relative_gap = float(solved.mip_gap)
    if solved.x is None:
        return result
    if solved.status == 0 and result.lower_bound_mln is None:
        # HiGHS omits MIP statistics when presolve reduces the problem to an LP.
        result.lower_bound_mln = float(solved.fun + constant)
        result.relative_gap = 0.0
    x = solved.x
    investments = [Investment(investment_id=kind, decision_date=f"{year}-01-01") for kind, opts in choices.items() for year, idx in opts.items() if x[idx] > .5]
    reservations = [Reservation(source_id=sid, year=year, annual_capacity_t=float(x[idx])) for (sid, year), idx in reserve_vars.items() if x[idx] > 1e-8]
    orders = []
    for sid, source in case.sources.items():
        lead = 0 if sid == "C" else source_lead(source, template)
        for day in range(n):
            volume = sum(x[flows[sid, day, mode]] for mode in [0, 1])
            if volume > 1e-8:
                orders.append(Order(source_id=sid, order_date=day_label(day-lead, first), delivery_date=day_label(day, first), volume_t=float(volume), startup=day == 0))
    plan = Plan(plan_id=settings.plan_id, decisions=Decisions(supply_orders=orders, capacity_reservations=reservations, investments=investments), description="Минимальная стоимость BASE при 100% обслуживании; инвестиции на границе года")
    if case.fingerprint != load_case().fingerprint:
        plan.case_snapshot = case_snapshot(case)
    result.objective_mln = float(solved.fun + constant)
    checked = evaluate(case, plan)
    delta = abs(checked.total_cost-result.objective_mln)
    full_service = all(row["shortage_t"] <= 1e-5 for row in checked.payload["yearly_balance"])
    if checked.feasible and full_service and delta <= max(1e-5, abs(result.objective_mln)*1e-8):
        result.plan, result.validated = plan, True
    else:
        result.status = "verification_failed"
        result.message = f"Проверка симулятором не пройдена: cost delta={delta}; violations={checked.payload['constraint_checks'][:5]}"
    return result
