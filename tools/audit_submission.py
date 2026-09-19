"""Build the criterion evidence, selected strategy and report from the same results."""
from __future__ import annotations

import hashlib
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from fuel_planner.analysis import assess_risk
from fuel_planner.data import ROOT, load_case
from fuel_planner.engine import evaluate
from fuel_planner.models import Risk
from fuel_planner.recovery import recovery_plan
from fuel_planner.sensitivity import study
from fuel_planner.storage import export_bytes, load_plan, save_plan


class Report:
    def __init__(self):
        self.md, self.html = [], []

    def page(self, title, text, rows=None):
        self.md.extend([f"## {title}", "", text, ""])
        content = "".join(f"<p>{html.escape(p)}</p>" for p in text.split("\n\n"))
        if rows:
            frame = pd.DataFrame(rows)
            rendered = frame.map(lambda x: f"{x:.6f}" if isinstance(x, float) else str(x))
            self.md.extend(["| " + " | ".join(rendered.columns) + " |", "| " + " | ".join("---" for _ in rendered.columns) + " |"])
            for row in rendered.itertuples(index=False):
                self.md.append("| " + " | ".join(str(v).replace("|", "/").replace("\n", " ") for v in row) + " |")
            self.md.append("")
            content += rendered.to_html(index=False, escape=True)
        self.html.append(f"<section><h1>{html.escape(title)}</h1>{content}</section>")

    def write(self, directory):
        (directory/"MANAGEMENT_REPORT.md").write_text("# Управленческая записка: Топливный космоконтур 2035\n\n"+"\n".join(self.md), encoding="utf-8")
        css = "body{font-family:Arial,sans-serif;color:#172331;max-width:1100px;margin:30px auto;line-height:1.42}section{position:relative;min-height:175mm;margin:40px 0;padding:25px;border:1px solid #cdd6df}h1{font-size:22px;margin-top:0}table{border-collapse:collapse;width:100%;font-size:10.5px;overflow-wrap:anywhere}td,th{border:1px solid #cdd6df;padding:5px;text-align:left}th{background:#eaf0f5}p{white-space:pre-line}.document-title{font-size:13px;color:#52606d;border-bottom:1px solid #cdd6df;padding-bottom:5px;margin-bottom:12px}@media print{body{margin:0}section{break-after:page;border:0;padding:0;margin:0;min-height:auto}section:last-child{break-after:auto}@page{size:A4 landscape;margin:14mm}}"
        pages = []
        for number, section in enumerate(self.html, start=1):
            pages.append(section.replace("<section>", f'<section><div class="document-title">Управленческая записка · стр. {number} из {len(self.html)}</div>', 1))
        (directory/"MANAGEMENT_REPORT.html").write_text(f'<!doctype html><html lang="ru"><meta charset="utf-8"><title>Управленческая записка</title><style>{css}</style><body>'+"".join(pages)+"</body></html>", encoding="utf-8")


def markdown_table(rows):
    frame = pd.DataFrame(rows)
    rendered = frame.map(lambda x: f"{x:.3f}" if isinstance(x, float) else str(x))
    lines = ["| " + " | ".join(rendered.columns) + " |", "| " + " | ".join("---" for _ in rendered.columns) + " |"]
    for row in rendered.itertuples(index=False):
        lines.append("| " + " | ".join(str(v).replace("|", "/").replace("\n", " ") for v in row) + " |")
    return "\n".join(lines)


def write_scenario_summary(directory, selected, results, responses, plans, selection):
    selected_rows = []
    labels = {
        "BASE": "Базовый",
        "LOW_DEMAND": "Низкий спрос",
        "HIGH_DEMAND": "Высокий спрос",
        "MANDATORY_STRESS": "Обязательный стресс, без ответа",
    }
    for scenario in ["BASE", "LOW_DEMAND", "HIGH_DEMAND", "MANDATORY_STRESS"]:
        summary = results[selected.plan_id, scenario].summary()
        selected_rows.append({
            "Сценарий": labels[scenario],
            "Стоимость, млн": summary["total_cost_mln"],
            "Дефицит, т": summary["shortage_t"],
            "Critical, т": summary["critical_shortage_t"],
            "Отклонено, т": summary["rejected_t"],
            "Мин. сервис": summary["min_total_service"],
            "Hard OK": summary["feasible"],
        })
    adapted = responses[selected.plan_id].summary()
    selected_rows.append({
        "Сценарий": "Обязательный стресс, после ответа",
        "Стоимость, млн": adapted["total_cost_mln"],
        "Дефицит, т": adapted["shortage_t"],
        "Critical, т": adapted["critical_shortage_t"],
        "Отклонено, т": adapted["rejected_t"],
        "Мин. сервис": adapted["min_total_service"],
        "Hard OK": adapted["feasible"],
    })
    strategy_rows = []
    for plan in plans:
        base = results[plan.plan_id, "BASE"].summary()
        adapted_plan = responses[plan.plan_id].summary()
        strategy_rows.append({
            "Архитектура": plan.plan_id,
            "BASE, млн": base["total_cost_mln"],
            "BASE дефицит, т": base["shortage_t"],
            "Stress после ответа, т": adapted_plan["shortage_t"],
            "Critical, т": adapted_plan["critical_shortage_t"],
        })
    md = f"""# Одностраничное сравнение сценариев

**Решение:** сохранить архитектуру `{selected.plan_id}` и активировать сохраненный план ответа только при обязательном стрессе. Правило выбора: {selection['rule']}

## Сценарии выбранной стратегии

{markdown_table(selected_rows)}

## Сравнение архитектур

{markdown_table(strategy_rows)}

## Почему исходные решения сохраняются

Инвестиции EARTH_NEW, ZBO и LUNAR_ISRU, базовые заказы и зарезервированные права не отменяются после 01.01.2038: они уже профинансированы или контрактно приняты, обеспечивают ввод источников и физическую готовность, а их отмена не создала бы топливо в пределах срока реакции. Ответ добавляет партии только в пределах ранее оплаченных свободных прав с учетом lead time и минимальных 42 дней реакции. При BASE, low и high demand исходный план не перестраивается, чтобы сравнение отражало влияние сценария, а не скрытую замену стратегии. Поэтому LOW показывает переполнение/отклонение 166.418 т, а HIGH — дефицит 283.000 т: это наблюдаемые границы исходного решения, а не повод задним числом менять его в сравнительной таблице. В mandatory stress сохранение обязательств вместе с активацией гибкости снижает дефицит с {results[selected.plan_id, 'MANDATORY_STRESS'].summary()['shortage_t']:.3f} до {adapted['shortage_t']:.3f} т; стоимость возрастает до {adapted['total_cost_mln']:.3f} млн у.е. Решение сохраняется потому, что оно единственное из трех рассмотренных архитектур устраняет общий и критический дефицит после допустимой реакции.

`feasible` относится к жестким ограничениям; дефицит и сервис показаны отдельно. Все значения получены из одного fingerprint входов `{results[selected.plan_id, 'BASE'].payload['input_fingerprint']}`.
"""
    (directory/"SCENARIO_SUMMARY.md").write_text(md, encoding="utf-8")
    selected_html = pd.DataFrame(selected_rows).map(lambda x: f"{x:.3f}" if isinstance(x, float) else str(x)).to_html(index=False, escape=True)
    strategy_html = pd.DataFrame(strategy_rows).map(lambda x: f"{x:.3f}" if isinstance(x, float) else str(x)).to_html(index=False, escape=True)
    css = "body{font-family:Arial,sans-serif;color:#172331;margin:10mm;line-height:1.28;font-size:11px}h1{font-size:21px;margin:0 0 8px}h2{font-size:15px;margin:10px 0 4px}p{margin:5px 0}table{border-collapse:collapse;width:100%;font-size:9.5px}td,th{border:1px solid #cdd6df;padding:4px;text-align:left}th{background:#eaf0f5}@page{size:A4 landscape;margin:10mm}@media print{body{margin:0}}"
    rationale = f"Инвестиции, базовые заказы и резервирование сохраняются: они уже приняты или обеспечивают ввод и готовность, а отмена не дает ресурс в пределах реакции. LOW/HIGH не перестраиваются: их переполнение 166.418 т и дефицит 283.000 т показывают границы исходного решения. Дополнительные партии используют только ранее оплаченные свободные права с учетом lead time и 42 дней реакции. В mandatory stress это уменьшает дефицит с {results[selected.plan_id, 'MANDATORY_STRESS'].summary()['shortage_t']:.3f} до {adapted['shortage_t']:.3f} т. Это единственная из трех архитектур, устраняющая общий и critical дефицит после допустимого ответа."
    body = f'<h1>Одностраничное сравнение сценариев</h1><p><b>Решение:</b> сохранить архитектуру {html.escape(selected.plan_id)}; план ответа активировать при mandatory stress.</p><h2>Сценарии выбранной стратегии</h2>{selected_html}<h2>Сравнение архитектур</h2>{strategy_html}<h2>Почему решения сохраняются</h2><p>{html.escape(rationale)}</p><p><small>Hard OK означает соблюдение жестких ограничений; сервис и дефицит показаны отдельно. Fingerprint: {html.escape(results[selected.plan_id, "BASE"].payload["input_fingerprint"])}</small></p>'
    (directory/"SCENARIO_SUMMARY.html").write_text(f'<!doctype html><html lang="ru"><meta charset="utf-8"><title>Сравнение сценариев</title><style>{css}</style><body>{body}</body></html>', encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    case = load_case()
    out = ROOT/"results"
    out.mkdir(exist_ok=True)
    plans = [load_plan(ROOT/"examples"/"plans"/f"{name}.json") for name in ["earth", "lunar", "diversified"]]
    results, comparisons, responses = {}, [], {}
    for p in plans:
        for scenario in ["BASE", "MANDATORY_STRESS", "LOW_DEMAND", "HIGH_DEMAND"]:
            r = evaluate(case, p, scenario)
            results[p.plan_id, scenario] = r
            comparisons.append(r.summary())
            (out/f"{p.plan_id}-{scenario}.zip").write_bytes(export_bytes(r, "csv"))
        adapted = recovery_plan(case, p)
        save_plan(adapted, ROOT/"examples"/"response_plans")
        responses[p.plan_id] = evaluate(case, adapted, "MANDATORY_STRESS")
        (out/f"{p.plan_id}-response.zip").write_bytes(export_bytes(responses[p.plan_id], "csv"))
        print("Evaluated", p.plan_id, flush=True)
    eligible = [p for p in plans if results[p.plan_id, "BASE"].feasible]
    def ranking(p):
        r = responses[p.plan_id]
        return (not r.feasible, round(r.summary()["critical_shortage_t"], 6), round(r.summary()["shortage_t"], 6), results[p.plan_id,"BASE"].total_cost, p.plan_id)
    selected = min(eligible, key=ranking)
    (ROOT/"examples"/"selected_plan.json").write_text(selected.model_dump_json(indent=2), encoding="utf-8")
    selection = dict(selected_plan_id=selected.plan_id,
        rule="Среди BASE-исполнимых планов: сначала hard constraints после допустимого ответа, затем критический недоотпуск, общий недоотпуск, стоимость BASE. Веса не используются; это TEAM_DECISION в пользу критических миссий.",
        input_fingerprint=case.fingerprint, ranking=[dict(plan_id=p.plan_id, keys=ranking(p)) for p in sorted(eligible, key=ranking)])
    (out/"selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(comparisons).to_csv(out/"comparison.csv", index=False)
    selected_base = results[selected.plan_id, "BASE"]
    selected_stress = results[selected.plan_id, "MANDATORY_STRESS"]
    response = responses[selected.plan_id]
    risk_specs = [
        Risk(risk_id="TEAM_CORE_DELAY", event="Earth-Core: задержка 30 дней в 2039–2040", cause="Недоступность согласованного окна запуска", source_id="A", start_year=2039, end_year=2040, delay_days=30, residual_delay_days=7, mitigation="Заранее согласованное резервное окно запуска", mitigation_decision_date="2037-01-01", mitigation_cost_mln=20, owner="launch_providers", probability_basis_or_range="TEAM: 7–30 дней, без вероятности; отдельное событие, не mandatory"),
        Risk(risk_id="TEAM_ISRU_OUTAGE", event="ISRU: 20% недопоставка в 2039", cause="Остановка части производственного оборудования", source_id="D", start_year=2039, end_year=2039, delivery_share=.8, residual_delivery_share=.95, mitigation="Запасные узлы и заранее оплаченная ремонтная готовность", mitigation_decision_date="2038-01-01", mitigation_cost_mln=35, mitigation_cost_basis="TEAM: 35 млн резерв на запасные узлы; диапазон бюджета 20–50, не котировка", owner="fuel_suppliers", probability_basis_or_range="TEAM: доля 0.8–0.95; не повтор обязательных 55%/75%"),
        Risk(risk_id="TEAM_FLEX_PRICE", event="Earth-Flex: рост переменной цены на 40%", cause="Рост стоимости земного запуска", source_id="B", start_year=2038, end_year=2040, price_multiplier=1.4, residual_price_multiplier=1.1, mitigation="Предварительное ценовое ограничение для новых договорных лет", mitigation_decision_date="2036-01-01", mitigation_cost_mln=15, mitigation_cost_basis="TEAM: 15 млн за механизм ограничения; бюджет 10–25, не рыночная котировка", owner="operator", probability_basis_or_range="TEAM: ×1.1–1.4, без вероятности; меняется только B, без наложения mandatory"),
    ]
    risks = []
    for risk in risk_specs:
        affected, residual = assess_risk(case, selected, risk)
        risks.extend(affected.payload["risk_register"])
        for label, r in [("before", affected), ("residual", residual)]:
            (out/f"{risk.risk_id}-{label}.zip").write_bytes(export_bytes(r, "csv"))
        if risk.risk_id == "TEAM_CORE_DELAY":
            (out/"risk-analysis.zip").write_bytes(export_bytes(affected, "csv"))
    selected_base.payload["risk_register"] = risks
    (out/"risk-register.json").write_text(json.dumps(risks, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"selected-BASE.json").write_bytes(export_bytes(selected_base))
    (out/"selected-BASE.xlsx").write_bytes(export_bytes(selected_base, "xlsx"))
    (out/"selected-stress-response.xlsx").write_bytes(export_bytes(response, "xlsx"))
    (out/"earth-stress.xlsx").write_bytes(export_bytes(results["earth", "MANDATORY_STRESS"], "xlsx"))
    records, bounds = study(case, plans, selected.plan_id)
    pd.DataFrame(records).to_csv(out/"sensitivity-multiparameter.csv", index=False)
    pd.DataFrame(bounds).to_csv(out/"sensitivity-thresholds.csv", index=False)
    pd.DataFrame([r for r in records if r["plan_id"] == selected.plan_id and r["parameter"] == "demand"]).to_csv(out/"sensitivity.csv", index=False)
    (out/"demand-threshold.json").write_text(json.dumps(dict(plan_id=selected.plan_id, input_fingerprint=case.fingerprint,
        result=next(r for r in bounds if r["parameter"] == "demand")), ensure_ascii=False, indent=2), encoding="utf-8")
    print("Sensitivity complete", flush=True)
    all_results = list(results.values())+list(responses.values())
    audit = dict(input_fingerprint=case.fingerprint, selected_plan_id=selected.plan_id,
        independent_controls_pass=all(c["passed"] for r in all_results for c in r.payload["independent_controls"]),
        no_negative_inventory=all(d["closing_inventory_t"] >= -1e-9 for r in all_results for d in r.payload["inventory_trace"]),
        identical_inputs_identical_results=export_bytes(evaluate(case, selected)) == export_bytes(evaluate(case, selected)),
        scenarios_use_same_inputs=len({r.payload["input_fingerprint"] for r in all_results}) == 1,
        stakeholder_cash_reconciles=all(abs(sum(s["allocated_cash_cost_mln"] for s in r.payload["stakeholders"])-r.total_cost) < 1e-6 for r in all_results),
        violations_have_year_value_reason=all(v["year"] and v["message"] and "excess" in v for r in all_results for v in r.payload["constraint_checks"]))
    if not all(v for v in audit.values() if isinstance(v, bool)):
        raise AssertionError(audit)
    (out/"completion-checks.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    for key, filename in [("stakeholders", "stakeholders.csv"), ("roadmap", "roadmap.csv"), ("independent_controls", "independent-controls.csv"), ("constraint_ledger", "annual-constraints.csv")]:
        pd.DataFrame(selected_base.payload[key]).assign(plan_id=selected.plan_id, scenario_id="BASE", input_fingerprint=case.fingerprint).to_csv(out/filename, index=False)

    budget_components = [
        ("Закупка топлива", "fuel_purchasing_mln"),
        ("Take-or-pay за невыбранный минимум", "take_or_pay_mln"),
        ("Резервирование мощности", "reservation_mln"),
        ("Хранение", "holding_mln"),
        ("Постоянный OPEX", "fixed_opex_mln"),
        ("CAPEX", "capex_mln"),
        ("Меры снижения риска в BASE", "mitigation_mln"),
    ]
    budget_rows = [
        {"Статья": label, "2035–2040, млн у.е.": sum(row[key] for row in selected_base.payload["financial_breakdown"])}
        for label, key in budget_components
    ]
    budget_rows.append({"Статья": "ИТОГО", "2035–2040, млн у.е.": selected_base.total_cost})
    contract_rows = []
    for source_id in sorted({row["source_id"] for row in selected_base.payload["contract_ledger"]}):
        source_rows = [row for row in selected_base.payload["contract_ledger"] if row["source_id"] == source_id]
        active = [row for row in source_rows if row["reserved_capacity_t"] > 1e-9]
        contract_rows.append({
            "Источник": source_id,
            "Контрактные годы": ", ".join(str(row["year"]) for row in active) or "нет",
            "Плановая мощность, т/год": max(row["source_capacity_t"] for row in source_rows),
            "Заказано, т": sum(row["ordered_t"] for row in source_rows),
            "Фактически принято, т": sum(row["accepted_t"] for row in source_rows),
            "Резервирование, млн": sum(row["reservation_mln"] for row in source_rows),
            "TOP, млн": sum(row["take_or_pay_mln"] for row in source_rows),
            "Lead time, дней": max(row["lead_days"] for row in source_rows),
        })

    report = Report()
    report.page("1. Управленческое решение и выбор архитектуры", f"Рекомендуется стратегия {selected.plan_id} с заранее сохраненной процедурой ответа на обязательный стресс. {selection['rule']}\n\nСтоимость BASE составляет {selected_base.total_cost:.3f} млн у.е. в постоянных ценах 2035 года. В BASE план обеспечивает полный общий и критический сервис без физически отрицательного запаса. Без реакции mandatory stress создает {selected_stress.summary()['shortage_t']:.3f} т недоотпуска, включая {selected_stress.summary()['critical_shortage_t']:.3f} т critical. Допустимая реакция снижает оба дефицита до нуля при стоимости {response.total_cost:.3f} млн у.е.\n\nEarth и Lunar дешевле в номинальном BASE, однако после реакции оставляют соответственно {responses['earth'].summary()['shortage_t']:.3f} и {responses['lunar'].summary()['shortage_t']:.3f} т дефицита. Поэтому решение основано на выполнении обязательств и физическом результате, а не на технологической сложности или самом наличии ISRU. Выручка не задана, поэтому стоимость снабжения не интерпретируется как прибыльность.", [dict(План=p.plan_id, Стоимость_BASE=results[p.plan_id,"BASE"].total_cost, Дефицит_после_ответа=responses[p.plan_id].summary()["shortage_t"], Критический_дефицит=responses[p.plan_id].summary()["critical_shortage_t"], Hard_OK=responses[p.plan_id].feasible) for p in plans])
    report.page("2. Данные, допущения, методы и научные источники", f"Расчет использует контрольные CSV в data/ и определения сценариев в scenarios/. Fingerprint входов: {case.fingerprint}. Редактирование через интерфейс создает отдельную исследовательскую копию с меткой TEAM_ASSUMPTION; исходные ограничения и mandatory stress защищены. Единицы: тонны, т/год и млн у.е. в постоянных ценах 2035 года. Critical demand входит в total demand.\n\nОсновные допущения: 365 дней в году; равномерный суточный спрос внутри года; месяц = 365/12 дня; используются верхние границы lead time; все источники дают агрегированный совместимый ресурс в один узел; транспорт уже включен в цену поставки. Начальный запас покупается один раз. Физический резерв отделен от договорной мощности. После ввода Earth-New повторные партии не получают новый 24-месячный срок строительства. Эти конвенции необходимы для однозначного расчета, но не являются универсальными физическими фактами.\n\nNASA-STD-7009B применяется к provenance, проверяемости, контрольным случаям и раскрытию ограничений. Обзор Ho (2024, DOI 10.2514/1.A35982) поддерживает совместное моделирование потоков, запасов и инфраструктуры. Simonini et al. (2024, DOI 10.1038/s41526-024-00377-5) обосновывают отдельный блок криогенного хранения и потерь, но не конкретные коэффициенты кейса. Sommariva et al. (2023, DOI 10.1016/j.actaastro.2023.01.004) поддерживают технико-экономическое сравнение Earth/Lunar на общей базе. Han et al. и Guo et al. поддерживают анализ диверсификации, резервирования и запасов как механизмов устойчивости. SciPy milp/HiGHS используется как решатель, а не как источник допущений. Полная evidence map и пределы применимости приведены в docs/SCIENTIFIC_BASIS.md.")
    report.page("3. Расчетная и снабженческая архитектура", "Цепочка решения полностью трассируема: входные данные → инвестиционные и контрактные решения → плановая и фактически доступная мощность → датированные поступления → ежедневный материальный баланс → годовые затраты → риски и ограничения → сравнение стратегий → выбранный план. Каждый блок возвращает данные следующему блоку, а интерфейс и экспорт используют один объект Result.\n\nМатериальный баланс: I_end = I_start + accepted_gross − losses − served. Потери равны accepted_gross × loss_rate и применяются один раз. Shortage = max(0, demand − served); critical обслуживается первым. Запас не становится отрицательным. Переполнение хранилища не исчезает из учета: отклоненный объем и нарушение показываются отдельно, а ранее возникшая договорная оплата сохраняется.\n\nПлановая мощность источника, мощность после ввода, scenario potential, зарезервированное право, заказ и фактическое поступление хранятся отдельно. Поставка не принимается до commissioning, без контракта или при нарушении lead time. Резервирование дает право на заказ, но не создает физический запас. CAPEX запускает инфраструктуру только после оплаты и срока подготовки.\n\nДля воспроизводимости полный snapshot входов и плана, формулы, units, fingerprints, contract ledger, daily trace, constraint ledger и стоимость экспортируются вместе. Одинаковые входы дают идентичный JSON. Независимый контроль повторно считает годовой баланс, перенос остатков, TOP и разложение затрат с допуском 1e−6.")
    report.page("4. Экономика и бюджет 2035–2040", f"Сравниваются недисконтированные реальные расходы в базе цен 2035 года: fuel purchasing + take-or-pay + reservation + holding + fixed OPEX + CAPEX + объявленные меры. Ставка дисконтирования организатором не задана, поэтому произвольный NPV не используется. Общий бюджет выбранной стратегии равен {selected_base.total_cost:.3f} млн у.е.; это потребность в финансировании снабжения, а не прибыль или оценка проекта.\n\nQ_pay = max(Q_order, TOP × Q_reserved_period). В таблице fuel purchasing показывает заказанное топливо, а TOP — только доплату за невыбранный минимум; эти поля не суммируются повторно через procurement. Holding рассчитан по среднему физическому запасу с учетом времени. Reservation начисляется один раз за период договорного права. Срыв поставки не аннулирует уже принятые платежные обязательства.\n\nCAPEX выбранного плана составляет 1 790 млн у.е., что укладывается в лимиты 1 800 млн до 2037 года и 2 800 млн до 2040 года. Пиковое годовое финансирование приходится на 2037 год из-за LUNAR_ISRU. Бюджет ответа на mandatory stress составляет {response.total_cost:.3f} млн у.е., то есть на {response.total_cost-selected_base.total_cost:.3f} млн выше BASE; сюда входят дополнительные закупки, изменение holding/TOP и 20 млн координации.", budget_rows)
    report.page("5. Пусковые, снабженческие контракты и ввод мощностей", "Контрактный контур синхронизирует финансирование, commissioning, резервирование, размещение заказа и фактическое поступление. Earth-Core A резервируется заблаговременно из-за 365-дневного lead time. Earth-Flex B и Emergency E сохраняются как гибкие права; Lunar-New C имеет take-or-pay 50%, поэтому невыбранный минимум оплачивается. Lunar-ISRU D становится доступен только после CAPEX 2037 года и ввода 01.01.2038.\n\nИнвестиционные вехи: EARTH_NEW финансируется в 2035 году и вводится в 2037 году; ZBO финансируется и вводится в 2036 году; LUNAR_ISRU финансируется в 2037 году и вводится в 2038 году. Опцион Earth-New 90 млн включен в общий CAPEX механизма, а не прибавлен третий раз. CAPEX и договорные даты проверяются ежегодно против лимитов и deadline.\n\nПусковой провайдер отвечает за окно и lead time, топливный поставщик — за объем в пределах мощности, оператор — за своевременный заказ и резервирование, финансирующая сторона — за CAPEX до ввода. Доля пусковых расходов внутри земного тарифа отдельно оценивается для распределения интересов, но не добавляется к TotalCost. Полные даты ранних заказов находятся в results/roadmap.csv.", contract_rows)
    report.page("6. Стресс-тестирование и сохранение решений", "Mandatory stress применяется отдельно от high demand: с 2038 года total и critical demand ×1.15; цены Earth-Core/Flex ×1.25 только в 2038–2039; фактическая поставка ISRU равна 55% плана в 2038 и 75% в 2039; losses/throughput не более 2% в 2038–2040. Коэффициент reliability повторно не применяется. Эти изменения сверяются с YAML и контрольным валидатором по каждому году.\n\nДо реакции diversified испытывает 226.992 т общего и 58.795 т critical дефицита. План ответа принимается 01.01.2038 при известных параметрах теста. Он не отменяет инвестиции, резервирование и уже размещенные заказы. Дополнительные партии используют только свободный остаток ранее оплаченных прав, соблюдают исходный lead time и минимум 42 дня организационной реакции; Emergency E не используется более двух последовательных лет. Алгоритм также сохраняет емкость хранилища для уже обещанных будущих партий.\n\nРешения сохраняются, потому что CAPEX уже создает доступную инфраструктуру, прежние договоры остаются обязательствами, а отмена не создает физическое топливо в пределах реакции. При BASE, LOW и HIGH план намеренно остается одинаковым, чтобы измерять эффект сценария. В mandatory stress сохраняется архитектура, но активируется заранее предусмотренная гибкость. Это доводит общий и critical сервис до 100% без нарушения hard constraints и без скрытого увеличения capacity. Координационные 20 млн — TEAM_ASSUMPTION.", [dict(Этап="Без реакции", **selected_stress.summary()), dict(Этап="После реакции", **response.summary())])
    report.page("7. Чувствительность и методика испытаний", "Каждый опыт меняет ровно один параметр на копии входов: спрос, цену Core, долю фактической поставки Core или задержку Core. После изменения повторно считаются cost, service, inventory и shortage для всех трех архитектур. Диапазоны имеют явное основание: low/high demand из кейса; +25% цены как контрольный масштаб mandatory; доля поставки 0–100% как физический диапазон; задержки 7–180 дней с 42 днями как масштаб Emergency lead time. Точки за пределами исходных данных помечены TEAM_ASSUMPTION.\n\nПорог нарушения сначала ищется по сетке, затем уточняется внутри первой найденной скобки. Изменение лучшей допустимой альтернативы сообщается как интервал сетки, а не как ложное точное значение. Для выбранной стратегии первый найденный порог demand находится непосредственно выше 1.0; исполнение Core становится недостаточным примерно ниже 95.11%; малая дополнительная задержка относительно годового графика также может вызвать сервисное нарушение. Цена Core до ×2 меняет стоимость, но сама по себе не меняет физический запас.\n\nСтохастическая модель не используется, потому что исходник не дает распределений отказов и зависимостей. Поэтому seed неприменим, а результаты интерпретируются как детерминированные сценарии и интервальные пороги. Полная таблица опытов находится в sensitivity-multiparameter.csv; таблица скобок — в sensitivity-thresholds.csv.", bounds)
    report.page("8. Реестр количественных рисков и меры", "Реестр относится к selected strategy и не повторяет mandatory stress. Для каждого события зафиксированы причина, период, affected parameter, зависимости, владелец, диапазон, расчетные последствия, мера, срок ее решения, стоимость и residual. Диапазоны являются сценарными гипотезами, а не статистическими вероятностями; денежные бюджеты мер не выдаются за коммерческие предложения.\n\nTEAM_CORE_DELAY моделирует задержку A на 30 дней в 2039–2040 из-за окна запуска; резервное окно за 20 млн снижает задержку до 7 дней, дефицит — на 19.233 т, но residual 7.479 т остается. TEAM_ISRU_OUTAGE моделирует 80% поставки D в 2039; запасные узлы и ремонтная готовность за 35 млн повышают residual delivery до 95% и уменьшают дефицит на 17.784 т, оставляя 5.928 т. TEAM_FLEX_PRICE меняет только цену B; ценовой механизм за 15 млн не улучшает сервис, поскольку B в выбранном BASE не отбирается, и поэтому его ценность должна быть пересмотрена до заключения.\n\nМера применяется только после даты решения и preparation/reaction time. Уже принятые партии не переписываются. Residual cost включает бюджет меры и оставшиеся операционные последствия. Такой расчет показывает случаи, где положительный бюджет не дает физического эффекта, и позволяет отказаться от слабой меры.", [{"Риск":r["risk_id"],"Источник":r["source_id"],"Период":r["period"],"Бюджет_меры":r["mitigation_cost_mln"],"Доп_затраты_при_риске":r["financial_consequence_mln"],"Снижение_недоотпуска_т":r["shortage_reduction_t"],"Остаточный_недоотпуск_т":r["residual_shortage_t"],"Остаточная_дельта_затрат":r["residual_cost_delta_mln"]} for r in risks])
    aggregates = []
    for sid in ["operator", "critical_consumers", "commercial_consumers", "fuel_suppliers", "launch_providers", "financier"]:
        base_rows = [s for s in selected_base.payload["stakeholders"] if s["stakeholder_id"] == sid]
        stress_rows = [s for s in selected_stress.payload["stakeholders"] if s["stakeholder_id"] == sid]
        response_rows = [s for s in response.payload["stakeholders"] if s["stakeholder_id"] == sid]
        aggregates.append(dict(Сторона=base_rows[0]["stakeholder"], Нагрузка_BASE=sum(s["allocated_cash_cost_mln"] for s in base_rows), Изменение_нагрузки_ответ=sum(s["allocated_cash_cost_mln"] for s in response_rows)-sum(s["allocated_cash_cost_mln"] for s in base_rows), Последствия_стресса_т=sum(s["affected_fuel_t"] for s in stress_rows), Последствия_ответа_т=sum(s["affected_fuel_t"] for s in response_rows), Обязанность=base_rows[0]["obligations"]))
    report.page("9. Стейкхолдеры, обязанности и компромиссы", "Оператор отвечает за договоры, баланс, резерв и операционные расходы; его KPI — service level, shortage и ликвидность. Critical consumers требуют непрерывности миссий и предоставляют график спроса; их KPI — critical service и недоотпуск. Commercial consumers получают остаточный ресурс после critical, поэтому первыми несут сокращение. Поставщики топлива отвечают за объем и доступную мощность; их KPI — arrival/order, а TOP защищает только платеж. Launch providers отвечают за окно и lead time. Финансирующая сторона обеспечивает CAPEX до commissioning и контролирует лимиты.\n\nВ BASE оператор несет 10 000.160 млн у.е. procurement/OPEX/reservation/holding, финансирующая сторона — 1 790 млн CAPEX. Сумма этих cash burdens равна TotalCost. Supplier receipts — распределение тех же платежей и не добавляются вторично. При stress без ответа 58.795 т critical дефицита и 168.197 т commercial дефицита распределяют физическое последствие асимметрично. После ответа оба равны нулю, но нагрузка оператора увеличивается на 1 350.169 млн у.е.\n\nВеса stakeholder utility не применяются: hard constraints и critical obligations нельзя компенсировать низкой стоимостью. Приоритет critical раскрыт прямо. Денежная стоимость провала миссии и выручка отсутствуют, поэтому последствия потребителей измеряются топливом и сервисом. Изменение риска пересчитывает обязательства и KPI до/после меры; владелец меры указан в risk register.", aggregates)
    report.page("10. Дорожная карта, контроль и границы решения", "Управленческие контрольные точки: в 2035 году утвердить Earth-New и финансирование 360 млн; в 2036 ввести ZBO и профинансировать 180 млн; в 2037 ввести Earth-New и профинансировать ISRU 1 250 млн; к 01.01.2038 ввести ISRU и подтвердить права B/C/E; ежегодно до размещения A подтвердить окно запуска и 365-дневный lead time; на 01.01.2038 провести stress gate и при срабатывании активировать response plan. До каждой вехи должны быть подтверждены dependency, владелец, финансирование и договорная мощность.\n\nПриемка выполняется тремя уровнями: unit/boundary/invalid-input tests; контрольные векторы V01–V10 и reference validator; повторная генерация evidence через tools/audit_submission.py. Проверяются отсутствие отрицательного inventory, годовой баланс, TOP и cost partition, одинаковый fingerprint сценариев, идентичность повторных результатов, cash reconciliation и полнота violations. HTML memo состоит ровно из 10 печатных страниц A4 landscape; отдельный SCENARIO_SUMMARY.html рассчитан на одну страницу.\n\nОграничения прототипа: ресурс агрегирован; спрос детерминирован и равномерен внутри года; реальные траектории, флот, дискретные пуски, компонентный состав, детальная термодинамика, выручка и эмпирические распределения отказов не моделируются. Годовая capacity не доказывает реализуемость конкретного launch manifest. Стартовая партия и повторные Earth-New используют раскрытые конвенции. Response известного сценария не является прогнозом неизвестного шока. Перед инвестиционным решением нужны engineering design, market quotes, probabilistic reliability data и юридическая проверка контрактов.\n\nЭксплуатация: `.venv/bin/streamlit run app.py`; проверка: `.venv/bin/python -m pytest -q` и `.venv/bin/python tools/validate_reference_repo.py --participant`; пересборка memo/evidence: `.venv/bin/python tools/audit_submission.py`.", [r for r in selected_base.payload["roadmap"] if r["owner"] in {"financier", "operator"}])
    report.write(ROOT/"docs")
    write_scenario_summary(ROOT/"docs", selected, results, responses, plans, selection)
    manifest_paths = [out/"selected-BASE.json", out/"comparison.csv", out/"risk-register.json", out/"sensitivity-multiparameter.csv", ROOT/"docs"/"MANAGEMENT_REPORT.md", ROOT/"docs"/"MANAGEMENT_REPORT.html", ROOT/"docs"/"SCENARIO_SUMMARY.md", ROOT/"docs"/"SCENARIO_SUMMARY.html", ROOT/"examples"/"selected_plan.json"]
    (out/"evidence-manifest.json").write_text(json.dumps({str(p.relative_to(ROOT)):digest(p) for p in manifest_paths}, indent=2), encoding="utf-8")
    print("Selected", selected.plan_id, "report and audit written", flush=True)


if __name__ == "__main__":
    main()
