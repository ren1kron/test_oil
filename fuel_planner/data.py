from __future__ import annotations

import copy
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Case:
    demand: dict[int, dict]
    sources: dict[str, dict]
    storage: dict[str, dict]
    investments: dict[str, dict]
    constraints: list[dict]
    scenarios: dict[str, dict]
    assumptions: list[str]

    @property
    def years(self):
        return sorted(self.demand)

    @property
    def fingerprint(self):
        # YAML permits integer year keys alongside a string "default" key.
        normalized = json.loads(json.dumps(self.__dict__, ensure_ascii=False))
        return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_case(root: Path | str = ROOT) -> Case:
    root = Path(root)

    def rows(name, numbers):
        with (root / "data" / f"{name}.csv").open(encoding="utf-8") as f:
            result = list(csv.DictReader(f))
        for row in result:
            for key in numbers:
                row[key] = float(row[key]) if row[key] else None
        return result

    demand_rows = rows("demand", ["year", "base_total_t", "base_critical_t", "low_total_t", "high_total_t"])
    source_rows = rows("supply_sources", ["capacity_t_per_year", "variable_cost_mln_per_t", "reservation_rate_mln_per_t_year_capacity", "take_or_pay_share", "lead_time_min_value", "lead_time_max_value", "available_from_year"])
    storage_rows = rows("storage_options", ["capacity_t", "loss_rate_on_throughput", "holding_cost_mln_per_t_year", "capex_mln", "fixed_opex_mln_per_year", "available_from_year"])
    investment_rows = rows("investment_options", ["option_fee_mln", "exercise_cost_mln", "total_capex_mln", "fixed_opex_mln_per_year"])
    constraint_rows = rows("constraints", ["value"])
    scenario_rows = [yaml.safe_load(p.read_text()) for p in sorted((root / "scenarios").glob("*.yaml"))]
    for records, key in [(demand_rows, "year"), (source_rows, "source_id"), (storage_rows, "storage_id"), (investment_rows, "investment_id")]:
        if len({r[key] for r in records}) != len(records):
            raise ValueError(f"Повторяющиеся значения {key}")
    result = Case(
        {int(r["year"]): r for r in demand_rows},
        {r["source_id"]: r for r in source_rows},
        {r["storage_id"]: r for r in storage_rows},
        {r["investment_id"]: r for r in investment_rows},
        constraint_rows, {r["scenario_id"]: r for r in scenario_rows}, [],
    )
    validate_case(result)
    return result


def validate_case(case: Case):
    import math
    if not case.years or case.years != list(range(case.years[0], case.years[-1] + 1)):
        raise ValueError("Годы спроса должны быть непрерывны")
    for row in case.demand.values():
        if not 0 <= row["base_critical_t"] <= row["base_total_t"]:
            raise ValueError("Критический спрос должен входить в общий")
    for group in [case.demand.values(), case.sources.values(), case.storage.values(), case.investments.values()]:
        for row in group:
            for key, value in row.items():
                if isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
                    raise ValueError(f"Некорректное числовое поле: {key}")
    for s in case.sources.values():
        if not 0 <= s["take_or_pay_share"] <= 1 or s["lead_time_min_value"] > s["lead_time_max_value"]:
            raise ValueError("Некорректные параметры поставщика")
        if s["lead_time_unit"] not in {"day", "week", "month", "year"}:
            raise ValueError("Неизвестная единица lead time")
    if len({s["name"] for s in case.sources.values()}) != len(case.sources):
        raise ValueError("Имена источников должны быть уникальны для привязки сценария")
    for s in case.storage.values():
        if not 0 <= s["loss_rate_on_throughput"] < 1:
            raise ValueError("Некорректные потери хранилища")
    if not {"BASE", "MANDATORY_STRESS"} <= case.scenarios.keys():
        raise ValueError("Отсутствуют обязательные сценарии")
    for scenario in case.scenarios.values():
        for field in ["demand_multiplier", "critical_demand_multiplier", "variable_price_multiplier", "actual_delivery_share"]:
            def check_values(mapping):
                if not isinstance(mapping, dict):
                    raise ValueError(f"{field}: требуется таблица множителей")
                for value in mapping.values():
                    if isinstance(value, dict):
                        check_values(value)
                    elif not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0 or (field == "actual_delivery_share" and value > 1):
                        raise ValueError(f"Некорректный множитель {field}")
            check_values(scenario.get(field, {}))
        for year, demand in case.demand.items():
            total_map = scenario.get("demand_multiplier", {})
            critical_map = scenario.get("critical_demand_multiplier", {})
            total = demand["base_total_t"] * total_map.get(year, total_map.get("default", 1))
            critical = demand["base_critical_t"] * critical_map.get(year, critical_map.get("default", 1))
            if critical > total:
                raise ValueError("Критический спрос сценария превышает общий")


def research_case(case: Case, extra_source=False, future_year=False) -> Case:
    c = copy.deepcopy(case)
    if extra_source:
        c.sources["X"] = dict(c.sources["B"], source_id="X", name="Source-X", capacity_t_per_year=40.0, variable_cost_mln_per_t=10.0, status="TEAM_ASSUMPTION")
        c.assumptions.append("Source-X: 40 т/год, 10 млн/т; прочие параметры как Earth-Flex; синтетический пример")
    if future_year:
        previous = c.years[-1]
        year = previous + 1
        row = dict(c.demand[previous], year=year, status="TEAM_ASSUMPTION")
        for key in ["base_total_t", "base_critical_t", "low_total_t", "high_total_t"]:
            row[key] *= 1.05
        c.demand[year] = row
        c.assumptions.append(f"{year}: спрос +5%; цены, доступная мощность и физические нормы как в {previous}; reliability только metadata; без переноса конечных CAPEX/stress лимитов")
        for scenario in c.scenarios.values():
            for key in ["demand_multiplier", "critical_demand_multiplier"]:
                scenario.setdefault(key, {})[year] = 1.0
    validate_case(c)
    return c
