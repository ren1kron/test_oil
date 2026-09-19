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
        css = "body{font-family:Arial,sans-serif;color:#172331;max-width:1100px;margin:30px auto;line-height:1.5}section{margin:40px 0;padding:25px;border:1px solid #cdd6df}h1{font-size:23px}table{border-collapse:collapse;width:100%;font-size:11px;overflow-wrap:anywhere}td,th{border:1px solid #cdd6df;padding:6px;text-align:left}th{background:#eaf0f5}p{white-space:pre-line}@media print{body{margin:0}section{break-after:page;border:0;padding:0;margin:0}section:last-child{break-after:auto}@page{size:A4 landscape;margin:15mm}}"
        (directory/"MANAGEMENT_REPORT.html").write_text(f'<!doctype html><html lang="ru"><meta charset="utf-8"><title>Управленческая записка</title><style>{css}</style><body>'+"".join(self.html)+"</body></html>", encoding="utf-8")


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

    report = Report()
    report.page("1. Решение и основание выбора", f"Рекомендуется план {selected.plan_id} с отдельно сохраненной процедурой ответа. {selection['rule']}\n\nСтоимость BASE: {selected_base.total_cost:.6f} млн у.е. в постоянных ценах 2035. Без реакции обязательный стресс дает {selected_stress.summary()['shortage_t']:.6f} т недоотпуска; после реакции — {response.summary()['shortage_t']:.6f} т при стоимости {response.total_cost:.6f} млн. Надежность здесь измерена сценарием и сервисом, а не вымышленной вероятностью.\n\nВыбор дороже самого дешевого BASE и не является универсальным optimum. Его основание — защита критического потребителя при заданном тесте и наличии оплаченных контрактных прав. Веса сторон не скрывают жесткие требования. Ни выручка, ни прибыль в исходнике не заданы.", [dict(План=p.plan_id, Стоимость_BASE=results[p.plan_id,"BASE"].total_cost, Дефицит_после_ответа=responses[p.plan_id].summary()["shortage_t"], Критический_дефицит=responses[p.plan_id].summary()["critical_shortage_t"], Hard_OK=responses[p.plan_id].feasible) for p in plans])
    report.page("2. Данные, происхождение и граница модели", f"Использованы CSV data/ и сценарии scenarios/ без изменения контрольных значений. Fingerprint: {case.fingerprint}. Изменения в редакторе создают отдельную копию с TEAM_ASSUMPTION и сохраняются в плане.\n\nЕдиницы: т, т/год, млн у.е. в ценах 2035. Critical входит в total. Все источники поставляют агрегированный совместимый ресурс в один узел; доставка уже включена в цену. Первичные DOCX/XLSX в комплекте отсутствуют.\n\nДопущения: 365 дней, равномерный спрос, верхние lead times, месяц = 365/12 дня. Начальный запас оплачивается как стартовая партия и учитывается один раз. Резерв физический, а не договорная мощность. Earth-New имеет срок подготовки; повторные поставки после ввода не получают второй срок 24 месяца. Реальный рейсовый график, баковая термодинамика и компонентный состав не моделируются.")
    report.page("3. Архитектура и трассируемость", "Цепочка: Case + Plan → даты ввода и действующие договоры → заказы и фактические поступления → ежедневный баланс → годовые расходы → риск и проверки → альтернативы → выбранный план.\n\nЗагрузка и валидация: fuel_planner/data.py и models.py. Физика, договоры и финансы: engine.py. MILP: optimizer.py. Доказательные таблицы: evidence.py. Ответ с сохранением обязательств: recovery.py. Чувствительность: sensitivity.py. Интерфейс и экспорт используют те же объекты Result.\n\nМатериальный алгоритм: I_end = I_start + accepted_gross − losses − served. Loss = accepted_gross × rate один раз. Избыточный поток явно отклоняется с нарушением; его закупка остается оплаченной. При дефиците critical обслуживается первым, отрицательный запас запрещен. Поставка до ввода/без договора/с неверным lead time не принимается.\n\nКаждый экспорт содержит полные входы и план, fingerprints, сценарий, units, assumptions, trace, cost, contracts, checks, roadmap и stakeholders. Повторная оценка идентичных входов дает идентичный JSON.")
    report.page("4. Экономика и независимые проверки", "Сравниваются недисконтированные реальные расходы 2035–2040: Procurement + Reservation + Holding + FixedOPEX + CAPEX + явно объявленные расходы меры. Discount rate не предполагается. Procurement = fuel purchasing + только невыбранный TOP; вложенное поле procurement_mln не суммируется с этими двумя частями повторно.\n\nДля года: Q_pay = max(Q_order, TOP × Q_reserved_period). Holding — средний запас с учетом времени, не годовой спрос. Все отклонения от договора и недопоставки не отменяют прежние платежные обязательства. Суммы не называются прибылью.\n\nКонтроль независимо пересчитывает баланс через math.fsum и TOP как оплату заказа плюс невыбранный минимум; дополнительно есть аналитический тест одногодичного optimum и векторы V01–V10. Допуск баланса 1e-6; аналогичный допуск бухгалтерской сверки.", selected_base.payload["independent_controls"])
    report.page("5. Инвестиции, договоры и дорожная карта", "Согласовать инвестиции и договорные права до первой зависимой поставки. CAPEX не превышает 1800 до 2037 и 2800 до 2040. Дополнительные права B/C/E оплачены в BASE; они не создают топливо без нового заказа и срока доставки. TOP C учитывается даже при невыборке.\n\nФинансирование отвечает за CAPEX; оператор — за закупку и эксплуатацию. Это допущение распределения финансовой нагрузки, не заявление о подписанных договорах. Для каждой фактической даты оплаты и ввода полная таблица сохранена в results/roadmap.csv; договоры и их ранние заказы — в contract_ledger и roadmap экспорта.", [r for r in selected_base.payload["roadmap"] if r["owner"] in {"financier", "operator"}])
    report.page("6. Обязательный стресс и ответ", "С 2038 общий и critical спрос ×1.15; Core/Flex price ×1.25 только в 2038–2039; ISRU 55%/75% в 2038/2039; loss ceiling 2% с 2038 по 2040. Reliability повторно не умножается, high demand не накладывается.\n\nПлан ответа принимается 01.01.2038 с известными параметрами этого детерминированного теста. Исходные заказы, инвестиции и резервирование неизменны. Дополнительные партии используют остаток прежних договоров, их lead time и минимум 42 дня реакции. E ограничен первыми двумя годами ответа. Алгоритм сохраняет емкость для уже обещанных будущих партий. Координация 20 млн — TEAM_ASSUMPTION. Это условный план при известных шоках, не доказательство реакции на неизвестный будущий риск.\n\nСтоимость ответа включает фактические дополнительные закупки, изменение holding/TOP и координацию. Если свободных договорных прав нет, алгоритм не повышает capacity или бюджет и честно показывает отсутствие улучшения.", [dict(Этап="Без реакции", **selected_stress.summary()), dict(Этап="После реакции", **response.summary())])
    report.page("7. Чувствительность и методика испытаний", "Каждый опыт меняет ровно один параметр на копии входов, сохраняет планы и пересчитывает cost, service, inventory, shortages. Диапазоны и происхождение перечислены в таблице. Граница hard/service-нарушения сначала ищется сеткой, затем уточняется в первой найденной скобке; глобальная монотонность не утверждается. Смена лучшей по стоимости допустимой альтернативы отмечается интервалом сетки. Если граница не найдена в диапазоне, это не доказательство ее отсутствия за диапазоном.\n\nРекомендуемый план заранее платит за гибкость и не обязан выигрывать номинальное сравнение затрат. Полный набор чисел и названия более дешевых допустимых альтернатив: sensitivity-multiparameter.csv. Стохастические расчеты не выполняются: seed/distributions неприменимы; вероятности не выдумываются.", bounds)
    report.page("8. Риски, стоимость мер и остаточный риск", "Реестр относится к выбранной стратегии. Ни одно событие не повторяет mandatory как отдельную оценку. Каждое меняет конкретный источник, период и параметр. Диапазоны сценарные, не статистические; финансовые бюджеты мер являются допущениями команды, не коммерческими предложениями.\n\nЦена подготовки меры начисляется в дату решения. Изменяются только заказы после даты решения + срока подготовки; уже размещенные партии не исправляются. Ценовое ограничение действует только на полностью вновь заключаемые годовые договоры. Суммарные расходы residual включают бюджет меры и остаточные операционные последствия. Положительный бюджет не гарантирует улучшения — это проверяет модель.", [{"Риск":r["risk_id"],"Источник":r["source_id"],"Период":r["period"],"Бюджет_меры":r["mitigation_cost_mln"],"Доп_затраты_при_риске":r["financial_consequence_mln"],"Снижение_недоотпуска_т":r["shortage_reduction_t"],"Остаточный_недоотпуск_т":r["residual_shortage_t"],"Остаточная_дельта_затрат":r["residual_cost_delta_mln"]} for r in risks])
    aggregates = []
    for sid in ["operator", "critical_consumers", "commercial_consumers", "fuel_suppliers", "launch_providers", "financier"]:
        base_rows = [s for s in selected_base.payload["stakeholders"] if s["stakeholder_id"] == sid]
        stress_rows = [s for s in selected_stress.payload["stakeholders"] if s["stakeholder_id"] == sid]
        response_rows = [s for s in response.payload["stakeholders"] if s["stakeholder_id"] == sid]
        aggregates.append(dict(Сторона=base_rows[0]["stakeholder"], Нагрузка_BASE=sum(s["allocated_cash_cost_mln"] for s in base_rows), Изменение_нагрузки_ответ=sum(s["allocated_cash_cost_mln"] for s in response_rows)-sum(s["allocated_cash_cost_mln"] for s in base_rows), Последствия_стресса_т=sum(s["affected_fuel_t"] for s in stress_rows), Последствия_ответа_т=sum(s["affected_fuel_t"] for s in response_rows), Обязанность=base_rows[0]["obligations"]))
    report.page("9. Стейкхолдеры и компромиссы", "Учтены оператор, critical и commercial consumers, поставщики топлива, запусков и финансирующая сторона. Годовые интересы, KPI, обязанности, расходы и риски — в stakeholders.csv и каждом result export.\n\nСумма cash cost оператора и финансирующей стороны равна TotalCost. Receipts поставщиков — распределение этих платежей, не новые расходы. Доля запуска 30% земной закупки — отдельная аналитическая гипотеза; тарифы контрольного кейса уже включают запуск. Нет данных для денежного ущерба миссии, поэтому потребителям показан недоотпуск, а не вымышленный нулевой ущерб.\n\nКритический потребитель получает приоритет, коммерческий первым несет сокращение. Оплаченная гибкость повышает издержки оператора и инвестора, но уменьшает последующий физический ущерб. TOP защищает поступления поставщика при низком отборе. Веса не применяются. При изменении риска таблицы пересчитываются до/после меры.", aggregates)
    report.page("10. Проверка, эксплуатация и ограничения", "Запуск: .venv/bin/streamlit run app.py. Проверки: .venv/bin/python -m pytest -q; .venv/bin/python tools/validate_reference_repo.py --participant; .venv/bin/python tools/audit_submission.py. Сохранить/открыть план, изменить решение или исследовательские входы, пересчитать, экспортировать.\n\nОграничения: агрегированный ресурс, детерминированный спрос, нет траекторий/ракет/выручки/эмпирических распределений; годовые capacity не доказывают реальный рейсовый график. Стартовая партия и Earth-New требуют принятия явно раскрытой конвенции. Оптимизатор ищет BASE full service с годовыми датами инвестиций, не все возможные политики. Ответ использует известный сценарий, не предсказывает неизвестные шоки. Source-X/2041 остаются отдельным исследованием.\n\nМетодические источники: NASA-STD-7009B — проверяемость, критерии приемки и ограничения модели (https://standards.nasa.gov/standard/nasa/nasa-std-7009); SciPy milp/HiGHS — ограничения, integrality, termination и gap (https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html); локальные docs/CALCULATION_RULES.md и STRESS_PROTOCOL.md — обязательные формулы и сценарий. NASA не задает коэффициенты этого кейса, SciPy не обосновывает выбранные границы риска. Дополнительная научная библиография организатора — SCIENTIFIC_BASIS.md.", [dict(Проверка=k, Результат=v) for k,v in audit.items() if isinstance(v,bool)])
    report.page("Приложение. Одностраничное сравнение сценариев", "Одинаковая контрольная база и неизменный план внутри четырех сценариев. Feasible отражает hard limits; сервисные отклонения stress/low/high показаны отдельно. План ответа — отдельная явно обозначенная адаптация, не скрытая подмена стратегии.", [{"План":r["plan_id"],"Сценарий":r["scenario_id"],"Стоимость":r["total_cost_mln"],"Недоотпуск_т":r["shortage_t"],"Critical_недоотпуск_т":r["critical_shortage_t"],"SL_min":r["min_total_service"],"Critical_SL_min":r["min_critical_service"],"Hard_OK":r["feasible"]} for r in comparisons])
    report.write(ROOT/"docs")
    manifest_paths = [out/"selected-BASE.json", out/"comparison.csv", out/"risk-register.json", out/"sensitivity-multiparameter.csv", ROOT/"docs"/"MANAGEMENT_REPORT.md", ROOT/"docs"/"MANAGEMENT_REPORT.html", ROOT/"examples"/"selected_plan.json"]
    (out/"evidence-manifest.json").write_text(json.dumps({str(p.relative_to(ROOT)):digest(p) for p in manifest_paths}, indent=2), encoding="utf-8")
    print("Selected", selected.plan_id, "report and audit written", flush=True)


if __name__ == "__main__":
    main()
