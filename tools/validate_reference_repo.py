#!/usr/bin/env python3
"""Static integrity checks for the organizer reference repository.

This script validates the starter-kit artifacts only. It does not calculate or
recommend a participant supply strategy.
"""

from __future__ import annotations

import csv
import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def repository_files():
    """Do not inspect installed dependencies, Git internals, or local caches."""
    for directory, children, files in os.walk(ROOT):
        children[:] = [name for name in children if name not in {
            ".git", ".venv", ".idea", "__pycache__", ".pytest_cache", ".mypy_cache",
            ".ruff_cache", "build", "dist", "saved_plans"
        } and not name.endswith(".egg-info")]
        for name in files:
            yield Path(directory) / name


def fail(message: str) -> None:
    raise AssertionError(message)


def read_csv(path: str) -> list[dict[str, str]]:
    p = ROOT / path
    with p.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        fail(f"{path}: empty CSV")
    return rows


def check_json_yaml_syntax() -> None:
    for p in list((ROOT / "schemas").glob("*.json")) + list((ROOT / "examples").rglob("*.json")) + list((ROOT / "validation").glob("*.json")):
        json.loads(p.read_text(encoding="utf-8"))
    for p in (ROOT / "scenarios").glob("*.yaml"):
        yaml.safe_load(p.read_text(encoding="utf-8"))


def check_demand() -> None:
    rows = read_csv("data/demand.csv")
    expected = {
        2035: (100.0, 80.0, 80.0, 110.0),
        2036: (140.0, 105.0, 112.0, 154.0),
        2037: (190.0, 135.0, 152.0, 209.0),
        2038: (250.0, 170.0, 200.0, 312.5),
        2039: (320.0, 210.0, 256.0, 400.0),
        2040: (390.0, 250.0, 312.0, 487.5),
    }
    got: dict[int, tuple[float, float, float, float]] = {}
    for r in rows:
        y = int(r["year"])
        got[y] = tuple(float(r[k]) for k in ("base_total_t", "base_critical_t", "low_total_t", "high_total_t"))  # type: ignore[assignment]
        if float(r["base_critical_t"]) > float(r["base_total_t"]):
            fail(f"data/demand.csv: critical demand exceeds total in {y}")
        if r["status"] != "CASE_INPUT":
            fail(f"data/demand.csv: {y} status must be CASE_INPUT")
    if got != expected:
        fail(f"data/demand.csv does not match organizer control table: {got}")


def check_sources() -> None:
    rows = read_csv("data/supply_sources.csv")
    by_id = {r["source_id"]: r for r in rows}
    if set(by_id) != {"A", "B", "C", "D", "E"}:
        fail("data/supply_sources.csv: expected source IDs A-E")

    expected = {
        "A": ("Earth-Core", 190.0, 6.2, 0.45, 0.70, 12.0, 12.0, "month"),
        "B": ("Earth-Flex", 110.0, 8.9, 0.15, 0.00, 4.0, 4.0, "month"),
        "C": ("Earth-New", 130.0, 7.1, 0.30, 0.50, 18.0, 24.0, "month"),
        "D": ("Lunar-ISRU", 120.0, 3.0, 0.00, 0.00, 1.0, 2.0, "month"),
        "E": ("Emergency", 80.0, 13.8, 0.35, 0.00, 6.0, 6.0, "week"),
    }
    for source_id, exp in expected.items():
        r = by_id[source_id]
        got = (
            r["name"],
            float(r["capacity_t_per_year"]),
            float(r["variable_cost_mln_per_t"]),
            float(r["reservation_rate_mln_per_t_year_capacity"]),
            float(r["take_or_pay_share"]),
            float(r["lead_time_min_value"]),
            float(r["lead_time_max_value"]),
            r["lead_time_unit"],
        )
        if got != exp:
            fail(f"source {source_id} differs from control inputs: {got}")
        if r["status"] != "CASE_INPUT":
            fail(f"source {source_id}: status must be CASE_INPUT")
        if float(r["lead_time_max_value"]) < float(r["lead_time_min_value"]):
            fail(f"source {source_id}: max lead time below min")

    emergency = by_id["E"]
    if not (emergency["lead_time_min_value"] == emergency["lead_time_max_value"] == "6" and emergency["lead_time_unit"] == "week"):
        fail("Emergency lead time must remain six weeks")


def check_investments_and_constraints() -> None:
    investments = {r["investment_id"]: r for r in read_csv("data/investment_options.csv")}
    earth_new = investments["EARTH_NEW"]
    if (float(earth_new["option_fee_mln"]), float(earth_new["exercise_cost_mln"]), float(earth_new["total_capex_mln"])) != (90.0, 270.0, 360.0):
        fail("Earth-New must be 90 + 270 = 360")

    constraints = {r["constraint_id"]: r for r in read_csv("data/constraints.csv")}
    required = {
        "BASE_CRITICAL_SERVICE": 0.99,
        "BASE_TOTAL_SERVICE": 0.97,
        "CAPEX_2037": 1800.0,
        "CAPEX_2040": 2800.0,
        "RESERVE_45D": 45.0,
        "EMERGENCY_BASE_STREAK": 2.0,
        "STRESS_LOSS_LIMIT": 0.02,
    }
    for cid, value in required.items():
        if cid not in constraints or float(constraints[cid]["value"]) != value:
            fail(f"constraint {cid} missing or changed")


def check_mandatory_stress() -> None:
    s = yaml.safe_load((ROOT / "scenarios/mandatory_stress.yaml").read_text(encoding="utf-8"))
    checks = {
        ("demand_multiplier", 2038): 1.15,
        ("demand_multiplier", 2039): 1.15,
        ("demand_multiplier", 2040): 1.15,
        ("critical_demand_multiplier", 2038): 1.15,
        ("critical_demand_multiplier", 2039): 1.15,
        ("critical_demand_multiplier", 2040): 1.15,
    }
    for (section, year), expected in checks.items():
        if s[section][year] != expected:
            fail(f"mandatory stress: {section}[{year}] must be {expected}")
    for source in ("Earth-Core", "Earth-Flex"):
        if s["variable_price_multiplier"][source][2038] != 1.25 or s["variable_price_multiplier"][source][2039] != 1.25 or s["variable_price_multiplier"][source][2040] != 1.0:
            fail(f"mandatory stress price path wrong for {source}")
    if s["actual_delivery_share"]["Lunar-ISRU"] != {2038: 0.55, 2039: 0.75, 2040: 1.0}:
        fail("mandatory stress Lunar-ISRU delivery shares changed")
    if s["loss_ceiling"]["from_year"] != 2038 or s["loss_ceiling"]["max_losses_divided_by_throughput"] != 0.02:
        fail("mandatory stress loss ceiling changed")


def check_schemas() -> None:
    schema_dir = ROOT / "schemas"
    for p in schema_dir.glob("*.json"):
        schema = json.loads(p.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    plan_schema = json.loads((schema_dir / "plan.schema.json").read_text(encoding="utf-8"))
    empty_plan = json.loads((ROOT / "examples/empty_plan.json").read_text(encoding="utf-8"))
    Draft202012Validator(plan_schema).validate(empty_plan)


def check_validation_vectors() -> None:
    items = json.loads((ROOT / "validation/expected_checks.json").read_text(encoding="utf-8"))
    ids = {x["case_id"] for x in items}
    expected_ids = {f"V{i:02d}" for i in range(1, 11)}
    if ids != expected_ids:
        fail(f"validation vectors must contain V01-V10 exactly; got {sorted(ids)}")


def check_relative_markdown_links() -> None:
    link_re = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    problems: list[str] = []
    for md in (p for p in repository_files() if p.suffix == ".md"):
        text = md.read_text(encoding="utf-8")
        for target in link_re.findall(text):
            target = target.strip()
            if not target or target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target_path = target.split("#", 1)[0].split("?", 1)[0]
            if not target_path:
                continue
            candidate = (md.parent / target_path).resolve()
            try:
                candidate.relative_to(ROOT.resolve())
            except ValueError:
                problems.append(f"{md.relative_to(ROOT)} -> escapes repo: {target}")
                continue
            if not candidate.exists():
                problems.append(f"{md.relative_to(ROOT)} -> missing: {target}")
    if problems:
        fail("broken relative Markdown links:\n" + "\n".join(problems))


def check_no_ready_solution() -> None:
    banned_paths = ["app.py", "src", "notebooks", "results", "configs", "requirements.txt", "pyproject.toml", "pytest.ini"]
    found = [p for p in banned_paths if (ROOT / p).exists()]
    if found:
        fail("starter repo contains participant-solution artifacts: " + ", ".join(found))

    phrases = ["рекомендуем купить", "нужно выбрать lunar-isru", "лучший поставщик", "победная стратегия", "оптимальный объём earth-core", "инвестировать в 2036"]
    for p in repository_files():
        if not p.is_file() or p.suffix.lower() not in {".md", ".csv", ".yaml", ".yml", ".json"}:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in phrases:
            if phrase in text:
                fail(f"prescriptive competition solution phrase in {p.relative_to(ROOT)}: {phrase}")


def check_required_readme_content() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    required = ["Ключевой принцип", "CASE_INPUT", "TEAM_DECISION", "TEAM_ASSUMPTION", "I_end = I_start + Q_delivered - Losses - Q_served", "R_y = D_y * 45 / 365", "MANDATORY_STRESS", "55%", "75%", "90 + 270 = 360", "SL_critical >= 0.99", "SL_total >= 0.97", "GitHub"]
    missing = [x for x in required if x not in text]
    if missing:
        fail("README missing required concepts: " + ", ".join(missing))


def check_no_placeholders_or_secrets() -> None:
    secret_name_re = re.compile(r"(^|/)(\.env|id_rsa|id_ed25519|credentials?\.json)$", re.I)
    placeholder_re = re.compile(r"\b(TBD|TODO|FIXME)\b")
    for p in repository_files():
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if secret_name_re.search(rel):
            fail(f"secret-like file committed: {rel}")
        if p.suffix.lower() in {".md", ".csv", ".yaml", ".yml", ".json"}:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if placeholder_re.search(text):
                fail(f"placeholder token in {rel}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--participant", action="store_true", help="Allow implemented participant software; keep reference-data checks")
    args = parser.parse_args()
    checks = [check_json_yaml_syntax, check_demand, check_sources, check_investments_and_constraints, check_mandatory_stress, check_schemas, check_validation_vectors, check_relative_markdown_links, check_no_ready_solution, check_required_readme_content, check_no_placeholders_or_secrets]
    if args.participant:
        checks.remove(check_no_ready_solution)
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print(f"PASS all {len(checks)} reference-repository integrity checks")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1)
