"""Run with: streamlit run app.py"""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import plotly.express as px
import streamlit as st

from fuel_planner.analysis import assess_risk, compare, demand_threshold, sensitivity
from fuel_planner.data import ROOT, load_case, research_case, case_snapshot, case_from_snapshot, edit_case
from fuel_planner.engine import evaluate
from fuel_planner.models import Plan, Risk
from fuel_planner.optimizer import Settings, optimize
from fuel_planner.planning import generate_schedule
from fuel_planner.storage import export_bytes, load_plan, save_plan
from fuel_planner.recovery import recovery_plan
from fuel_planner.sensitivity import study

st.set_page_config(page_title="Топливный космоконтур 2035", page_icon="🛰️", layout="wide")

LABELS = {
    "year": "Год", "source_id": "Источник", "volume_t": "Объем, т", "annual_capacity_t": "Резерв мощности, т/год",
    "order_date": "Дата заказа", "delivery_date": "Дата поставки", "startup": "Стартовый запас", "contingency": "Активация резерва",
    "start_day": "Начало договора, день 0–364", "end_day": "Конец договора, день 1–365 (не включительно)",
    "investment_id": "Инвестиция", "decision_date": "Оплата / реализация", "option_date": "Оплата опциона",
    "demand_t": "Спрос, т", "served_t": "Обслужено, т", "shortage_t": "Дефицит, т", "delivered_t": "Принято, т",
    "losses_t": "Потери, т", "closing_inventory_t": "Запас на конец, т", "opening_inventory_t": "Запас на начало, т",
    "critical_demand_t": "Критический спрос, т", "critical_served_t": "Критический спрос обслужен, т",
    "total_service_level": "Общий уровень обслуживания", "critical_service_level": "Критический уровень обслуживания",
    "total_cost_mln": "Стоимость, млн", "procurement_mln": "Закупки, млн", "reservation_mln": "Резерв мощности, млн",
    "holding_mln": "Хранение, млн", "fixed_opex_mln": "Постоянный OPEX, млн", "capex_mln": "CAPEX, млн",
    "reserve_required_t": "Требуемый резерв, т", "reserve_opening_t": "Резерв при проверке, т", "rejected_t": "Отклонено, т",
    "scenario_id": "Сценарий", "plan_id": "План", "feasible": "Жесткие ограничения соблюдены",
    "min_total_service": "Мин. общий сервис", "min_critical_service": "Мин. критический сервис", "violations": "Замечания",
    "rule_id": "Правило", "period": "Период", "actual": "Факт", "limit": "Предел", "excess": "Отклонение",
    "severity": "Тип", "message": "Пояснение", "date": "Дата", "capacity_t": "Емкость, т", "loss_ratio": "Доля потерь",
    "demand_multiplier": "Множитель спроса",
    "fuel_purchasing_mln": "Топливо, млн", "take_or_pay_mln": "Невыбранный TOP, млн", "mitigation_mln": "Меры реагирования, млн",
    "source_capacity_t": "Номинальная мощность, т/год", "available_capacity_t": "Доступная мощность периода, т",
    "reserved_capacity_t": "Зарезервировано, т/год", "scenario_potential_t": "Потенциал сценария, т",
    "actually_arrived_t": "Фактическое прибытие, т", "accepted_t": "Принято в хранилище, т",
    "uncalled_contract_t": "Невыбранные права, т", "contracted_volume_t": "Договорный объем, т",
    "passed": "Проверка пройдена", "unit": "Единица", "operator": "Оператор сравнения",
    "flexible_rights_2038_t": "Неиспользованные права 2038, lead ≤122 дня, т",
}
SCENARIOS = {"BASE": "Базовый", "MANDATORY_STRESS": "Обязательный стресс", "LOW_DEMAND": "Низкий спрос", "HIGH_DEMAND": "Высокий спрос"}


def table(rows):
    st.dataframe(pd.DataFrame(rows).rename(columns=LABELS), hide_index=True, width="stretch")


def set_plan(plan):
    if plan.case_snapshot is not None:
        case_from_snapshot(plan.case_snapshot)
    st.session_state.plan = plan.model_dump()
    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1


def signature(plan, case, scenario):
    return hashlib.sha256((plan.model_dump_json()+case.fingerprint+scenario).encode()).hexdigest()


def clean_rows(frame):
    rows = [{key: (None if pd.isna(value) or value == "" else value) for key, value in row.items()} for row in frame.to_dict("records")]
    for row in rows:
        for key, default in {"startup": False, "contingency": False, "start_day": 0, "end_day": 365}.items():
            if key in row and row[key] is None:
                row[key] = default
    return rows


def edit_rows(label, rows, columns, key):
    st.markdown(f"**{label}**")
    config = {k: LABELS.get(k, k) for k in columns}
    for column in columns:
        if column in {"startup", "contingency"}:
            config[column] = st.column_config.CheckboxColumn(LABELS[column], default=False)
        elif column == "source_id":
            config[column] = st.column_config.SelectboxColumn(LABELS[column], options=list(case.sources), required=True)
        elif column == "investment_id":
            config[column] = st.column_config.SelectboxColumn(LABELS[column], options=["EARTH_NEW", "LUNAR_ISRU", "ZBO"], required=True)
        elif column in {"volume_t", "annual_capacity_t"}:
            config[column] = st.column_config.NumberColumn(LABELS[column], min_value=0.0, required=True)
        elif column in {"year", "start_day", "end_day"}:
            default = {"year": case.years[0], "start_day": 0, "end_day": 365}[column]
            config[column] = st.column_config.NumberColumn(LABELS[column], step=1, default=default, required=True)
    return st.data_editor(pd.DataFrame(rows, columns=columns), key=key, num_rows="dynamic", hide_index=True,
                          width="stretch", column_config=config)


if "plan" not in st.session_state:
    initial = ROOT / "examples" / "selected_plan.json"
    if not initial.exists():
        initial = ROOT / "examples" / "plans" / "earth.json"
    set_plan(load_plan(initial) if initial.exists() else Plan(plan_id="my-plan"))

st.title("Топливный космоконтур 2035")
st.caption("Снабжение орбитального узла · Проверяемые решения · Все денежные суммы — млн у.е. в ценах 2035 года")
dataset = st.sidebar.selectbox("Набор данных", ["Контрольный 2035–2040", "Исследование: Source-X", "Исследование: 2041", "Исследование: Source-X и 2041"])
case = research_case(load_case(), "Source-X" in dataset, "2041" in dataset)
scenario = st.sidebar.selectbox("Сценарий", list(SCENARIOS), format_func=SCENARIOS.get)
plan = Plan.model_validate(st.session_state.plan)
if plan.case_snapshot is not None:
    case = case_from_snapshot(plan.case_snapshot)
    st.sidebar.warning("Используется исследовательский снимок из плана; он сохраняется вместе с решениями.")
    if st.sidebar.button("Вернуть выбранный контрольный набор"):
        plan.case_snapshot = None
        set_plan(plan)
        st.rerun()
st.sidebar.caption(f"План: {plan.plan_id} · Данные: {case.fingerprint[:12]}")

paths = sorted((ROOT / "examples" / "plans").glob("*.json")) + sorted((ROOT / "saved_plans").glob("*.json"))
if "2041" in dataset and "Source-X" in dataset:
    paths += sorted((ROOT / "examples" / "research_plans").glob("*.json"))
if paths:
    selected = st.sidebar.selectbox("Сохраненные планы", paths, format_func=lambda p: f"{p.parent.name}/{p.stem}")
    if st.sidebar.button("Открыть план"):
        try:
            set_plan(load_plan(selected))
            st.rerun()
        except (ValueError, OSError) as exc:
            st.sidebar.error(str(exc))
upload = st.sidebar.file_uploader("Импорт плана JSON", type="json")
if upload is not None and st.sidebar.button("Загрузить JSON"):
    try:
        set_plan(Plan.model_validate_json(upload.getvalue()))
        st.rerun()
    except ValueError as exc:
        st.sidebar.error(f"План не загружен: {exc}")
if st.sidebar.button("Сохранить план"):
    try:
        path = save_plan(plan, ROOT / "saved_plans")
        st.sidebar.success(f"Сохранено: {path.name}")
    except OSError as exc:
        st.sidebar.error(str(exc))
st.sidebar.download_button("Скачать план JSON", plan.model_dump_json(indent=2), file_name=f"{plan.plan_id}.json", mime="application/json")
if st.sidebar.button("Новый пустой план"):
    set_plan(Plan(plan_id="my-plan"))
    st.rerun()

editor, results_tab, comparison, optimization, risks_tab, inputs_tab, stakeholder_tab, assumptions_tab = st.tabs(["План", "Результаты", "Сравнение", "Оптимизация", "Риски", "Редактор данных", "Стейкхолдеры и отчет", "Данные и допущения"])
pending_edits = False
with editor:
    version = st.session_state.editor_version
    name = st.text_input("Идентификатор плана", plan.plan_id, key=f"name_{version}")
    description = st.text_input("Описание", plan.description, key=f"description_{version}")
    funding = st.text_input("Источник финансирования стартового запаса", plan.assumptions.opening_stock_funding, key=f"funding_{version}")
    target = st.number_input("Целевой запас, дней (генератор расписания)", min_value=45.0, value=max(45.0, plan.decisions.inventory_policy.get("target_days", 45)), key=f"target_{version}")
    st.caption("Дата: ГГГГ-ММ-ДД. Объемы — валовое топливо до потерь. Календарь — 365 дней, без 29 февраля.")
    investments = edit_rows("Инвестиции", [v.model_dump() for v in plan.decisions.investments], ["investment_id", "decision_date", "option_date"], f"investments_{version}")
    st.caption("Коды инвестиций: EARTH_NEW, LUNAR_ISRU, ZBO. Опцион EARTH_NEW можно оплатить отдельно; остальные option_date оставьте пустыми.")
    reservations = edit_rows("Договоры и резервирование", [v.model_dump() for v in plan.decisions.capacity_reservations], ["source_id", "year", "annual_capacity_t", "start_day", "end_day"], f"reservations_{version}")
    with st.expander("Подробное расписание поставок", expanded=False):
        orders = edit_rows("Заказы", [v.model_dump() for v in plan.decisions.supply_orders], ["source_id", "order_date", "delivery_date", "volume_t", "startup", "contingency"], f"orders_{version}")
    candidate = plan.model_dump()
    candidate.update(plan_id=name, description=description)
    candidate["assumptions"]["opening_stock_funding"] = funding
    candidate["decisions"].update(investments=clean_rows(investments), capacity_reservations=clean_rows(reservations), supply_orders=clean_rows(orders), inventory_policy={"target_days": target})
    pending_edits = candidate != plan.model_dump()
    if pending_edits:
        st.warning("Есть непримененные изменения. Результаты относятся к ранее примененному плану.")
    if st.button("Применить изменения", type="primary"):
        try:
            edited = Plan.model_validate(candidate)
            evaluate(case, edited, scenario)
            set_plan(edited)
            st.rerun()
        except ValueError as exc:
            st.error(f"Изменения не применены: {exc}")
    with st.expander("Сформировать расписание из годовых объемов"):
        st.caption("Генератор распределяет валовой объем равномерно по доступным дням; стартовая партия входит в объем первого года. Исполнимость проверяется после расчета.")
        annual = pd.DataFrame([dict(source_id=o.source_id, year=int(o.delivery_date[:4]), volume_t=o.volume_t) for o in plan.decisions.supply_orders], columns=["source_id", "year", "volume_t"])
        if not annual.empty:
            annual = annual.groupby(["source_id", "year"], as_index=False).volume_t.sum()
        annual = st.data_editor(annual, num_rows="dynamic", hide_index=True, key=f"annual_{version}", column_config={k: LABELS[k] for k in annual.columns})
        if st.button("Сгенерировать расписание"):
            try:
                generated = generate_schedule(case, Plan.model_validate(candidate), clean_rows(annual))
                set_plan(generated)
                st.rerun()
            except (ValueError, TypeError) as exc:
                st.error(str(exc))

with results_tab:
    if st.button("Рассчитать", type="primary", disabled=pending_edits):
        try:
            with st.spinner("Расчет ежедневного баланса…"):
                st.session_state.result = evaluate(case, plan, scenario)
                st.session_state.result_signature = signature(plan, case, scenario)
        except ValueError as exc:
            st.error(str(exc))
    result = st.session_state.get("result")
    if result and (pending_edits or st.session_state.get("result_signature") != signature(plan, case, scenario)):
        st.warning("Результат устарел: примените изменения и выполните расчет снова.")
    elif result:
        summary = result.summary()
        a, b, c, d = st.columns(4)
        a.metric("Полная стоимость, млн", f"{summary['total_cost_mln']:,.2f}")
        b.metric("Дефицит, т", f"{summary['shortage_t']:.3f}")
        c.metric("Минимальный общий сервис", f"{summary['min_total_service']:.2%}")
        d.metric("Минимальный критический сервис", f"{summary['min_critical_service']:.2%}")
        if result.feasible:
            st.success("Жесткие ограничения соблюдены. Сервисные отклонения других сценариев показаны отдельно.")
        else:
            st.error("План нарушает жесткие ограничения — см. диагностику ниже.")
        years = pd.DataFrame(result.payload["yearly_balance"])
        st.plotly_chart(px.bar(years.rename(columns=LABELS), x="Год", y=["Спрос, т", "Обслужено, т"], barmode="group", title="Спрос и обслуживание"), width="stretch")
        daily = pd.DataFrame(result.payload["inventory_trace"]).rename(columns=LABELS)
        st.plotly_chart(px.line(daily, x="Дата", y=["Запас на конец, т", "Требуемый резерв, т", "Емкость, т"], title="Запас и емкость хранилища"), width="stretch")
        st.caption("Резерв проверяется на начало года; линия требования в течение года служит ориентиром.")
        flows = pd.DataFrame(result.payload["source_schedule"])
        if not flows.empty:
            flows["actual_year"] = flows.actual_date.str[:4]
            grouped = flows.groupby(["actual_year", "source_id"], as_index=False).delivered_t.sum()
            st.plotly_chart(px.bar(grouped, x="actual_year", y="delivered_t", color="source_id", labels={"actual_year": "Год поступления", "delivered_t": "Принято, т", "source_id": "Источник"}, title="Поставки по источникам"), width="stretch")
        costs = pd.DataFrame(result.payload["financial_breakdown"]).rename(columns=LABELS)
        st.plotly_chart(px.bar(costs, x="Год", y=["Топливо, млн", "Невыбранный TOP, млн", "Резерв мощности, млн", "Хранение, млн", "Постоянный OPEX, млн", "CAPEX, млн", "Меры реагирования, млн"], title="Структура расходов"), width="stretch")
        table(result.payload["yearly_balance"])
        st.markdown("**Нарушения и сервисные отклонения**")
        table(result.payload["constraint_checks"])
        with st.expander("Полный годовой журнал проверок: факт, предел и результат"):
            table(result.payload["constraint_ledger"])
        with st.expander("Мощность, договоры и take-or-pay"):
            table(result.payload["contract_ledger"])
            table(result.payload["financial_breakdown"])
        with st.expander("Независимые сверки материального и финансового баланса"):
            table(result.payload["independent_controls"])
        for col, fmt, ext in zip(st.columns(3), ["json", "csv", "xlsx"], ["json", "zip", "xlsx"]):
            col.download_button(f"Экспорт {fmt.upper()}", export_bytes(result, fmt), file_name=f"{plan.plan_id}-{scenario}.{ext}", key=f"export_{fmt}")
    else:
        st.info("Нажмите «Рассчитать», чтобы проверить выбранный план и сценарий.")

with comparison:
    st.caption("Во всех сценариях используется один и тот же план. Обязательный стресс не объединяется с высоким спросом.")
    if st.button("Сравнить сценарии", disabled=pending_edits):
        try:
            st.session_state.comparison = (signature(plan, case, "compare"), compare(case, plan))
        except ValueError as exc:
            st.error(str(exc))
    comp = st.session_state.get("comparison")
    if comp and comp[0] == signature(plan, case, "compare") and not pending_edits:
        table(comp[1])
        st.download_button("Скачать сравнение CSV", pd.DataFrame(comp[1]).to_csv(index=False).encode("utf-8-sig"), "comparison.csv")
    if st.button("Исследовать чувствительность", disabled=pending_edits):
        try:
            st.session_state.sensitivity = (signature(plan, case, "sensitivity"), sensitivity(case, plan), demand_threshold(case, plan))
        except ValueError as exc:
            st.error(str(exc))
    if st.button("Сравнить стратегии и ключевые параметры", disabled=pending_edits):
        try:
            alternatives = [load_plan(ROOT/"examples"/"plans"/f"{name}.json") for name in ["earth", "lunar", "diversified"] if (ROOT/"examples"/"plans"/f"{name}.json").exists()]
            if all(p.plan_id != plan.plan_id for p in alternatives):
                alternatives.append(plan)
            with st.spinner("Одинаковые входы для всех альтернатив; расчет диапазонов и порогов…"):
                records, bounds = study(case, alternatives, plan.plan_id)
                st.session_state.strategy_study = (signature(plan, case, "study"), records, bounds)
        except ValueError as exc:
            st.error(str(exc))
    studied = st.session_state.get("strategy_study")
    if studied and studied[0] == signature(plan, case, "study") and not pending_edits:
        table(studied[1])
        table(studied[2])
        st.download_button("Экспорт чувствительности CSV", pd.DataFrame(studied[1]).to_csv(index=False).encode("utf-8-sig"), "sensitivity.csv")
    if st.button("Рассчитать ответ на обязательный стресс", disabled=pending_edits):
        try:
            response = recovery_plan(case, plan)
            before, after = evaluate(case, plan, "MANDATORY_STRESS"), evaluate(case, response, "MANDATORY_STRESS")
            st.session_state.response_result = (signature(plan, case, "response"), response, before, after)
        except ValueError as exc:
            st.error(str(exc))
    response_result = st.session_state.get("response_result")
    if response_result and response_result[0] == signature(plan, case, "response") and not pending_edits:
        _, response, before, after = response_result
        st.caption("Решение 01.01.2038; реакция минимум 42 дня и lead time источника. Все исходные обязательства сохраняются; используются только ранее оплаченные свободные права. Координация 20 млн — TEAM_ASSUMPTION.")
        table([dict(Этап="Без ответа", **before.summary()), dict(Этап="С ответом", **after.summary())])
        st.write(f"Изменение расходов: {after.total_cost-before.total_cost:,.2f} млн; остаточный дефицит: {after.summary()['shortage_t']:.3f} т")
        st.download_button("Скачать план ответа", response.model_dump_json(indent=2), "response.json")
        st.download_button("Экспорт ответа XLSX", export_bytes(after, "xlsx"), "response.xlsx")
    sens = st.session_state.get("sensitivity")
    if sens and sens[0] == signature(plan, case, "sensitivity") and not pending_edits:
        table(sens[1])
        st.caption("Порог — первое нарушение сервиса или годового резерва при росте спроса от BASE до ×2.")
        threshold = sens[2]
        if threshold["status"] == "found":
            st.info(f"Граница множителя спроса: {threshold['last_passing']:.5f}–{threshold['first_failing']:.5f}")
        else:
            st.info("BASE уже нарушает сервис/резерв" if threshold["status"] == "already_failing" else "До множителя ×2 нарушение не найдено")

with optimization:
    st.markdown("**Минимальная стоимость BASE при 100% обслуживании**")
    st.caption("Оптимизатор выбирает ежедневные поставки, договоры и инвестиции на границе года. После решения выполняется независимая симуляция. Устойчивость к стрессу проверяется отдельно.")
    disabled = st.multiselect("Отключить источники", list(case.sources), format_func=lambda s: case.sources[s]["name"])
    choices = {}
    for kind, label in [("EARTH_NEW", "Earth-New"), ("ZBO", "ZBO"), ("LUNAR_ISRU", "Lunar-ISRU")]:
        years = [2037] if kind == "LUNAR_ISRU" else [y for y in case.years if (kind != "ZBO" or y >= 2036) and (kind != "EARTH_NEW" or y+2 <= case.years[-1])]
        choices[kind] = st.selectbox(f"Инвестиция {label}", ["auto", "never"] + years, format_func=lambda v: {"auto": "Выбрать автоматически", "never": "Не инвестировать"}.get(v, str(v)), key=f"opt_{kind}")
    limit = st.number_input("Лимит решателя, секунд", min_value=1, max_value=3600, value=120)
    opt_signature = json.dumps([case.fingerprint, sorted(disabled), choices, limit], sort_keys=True)
    if st.button("Найти план", type="primary"):
        try:
            with st.spinner("Поиск плана и проверка результата…"):
                st.session_state.optimization = optimize(case, Settings(time_limit=float(limit), disabled_sources=disabled, investments=choices, plan_id="optimized"))
                st.session_state.optimization_case = case.fingerprint
                st.session_state.optimization_settings = opt_signature
        except ValueError as exc:
            st.error(str(exc))
    solved = st.session_state.get("optimization")
    if solved and st.session_state.get("optimization_settings") != opt_signature:
        st.warning("Настройки оптимизации изменены. Повторите поиск для новых условий.")
    elif solved and st.session_state.get("optimization_case") == case.fingerprint:
        statuses = {"optimal_within_gap": "Решение найдено в пределах заданного зазора", "time_limit": "Достигнут лимит времени", "infeasible": "Исполнимого плана нет", "verification_failed": "Независимая проверка не пройдена", "unbounded": "Неограниченная задача", "solver_error": "Ошибка решателя"}
        st.info(statuses.get(solved.status, solved.status))
        table([{"Статус": statuses.get(solved.status), "Время, с": solved.elapsed_seconds, "Стоимость, млн": solved.objective_mln, "Нижняя граница, млн": solved.lower_bound_mln, "Относительный зазор": solved.relative_gap, "Проверен": solved.validated}])
        if solved.plan:
            if st.button("Использовать найденный план"):
                set_plan(solved.plan)
                st.rerun()
            st.download_button("Скачать найденный план", solved.plan.model_dump_json(indent=2), "optimized.json")
        with st.expander("Сообщение решателя"):
            st.code(solved.message)

with risks_tab:
    st.caption("Исследовательский сценарий применяется отдельно к BASE. Вероятности не назначаются. Бюджет меры включается отдельной строкой; действие ограничено датой решения, сроком подготовки и уже принятыми обязательствами.")
    source = st.selectbox("Источник риска", list(case.sources))
    years = st.slider("Период риска", min_value=case.years[0], max_value=case.years[-1], value=(2038, 2039))
    share = st.slider("Доля поставки при риске", 0.0, 1.0, 0.75, step=0.05)
    delay = st.number_input("Задержка, дней", min_value=0, max_value=730, value=30)
    price = st.number_input("Множитель цены", min_value=0.01, value=1.0)
    owner = st.text_input("Владелец риска", "Оператор узла")
    mitigation = st.text_input("Мера снижения риска", "Сокращение задержки за счет резервного окна запуска")
    residual_delay = st.number_input("Задержка после меры, дней", min_value=0, max_value=730, value=7)
    residual_share = st.slider("Доля поставки после меры", 0.0, 1.0, 1.0, step=0.05)
    mitigation_cost = st.number_input("Стоимость меры, млн (TEAM_ASSUMPTION)", min_value=0.0, value=20.0)
    decision_date = st.text_input("Дата решения о мере", "2037-01-01")
    reaction_days = st.number_input("Срок подготовки меры, дней", min_value=0, value=42)
    cost_basis = st.text_input("Обоснование бюджета меры", "TEAM_ASSUMPTION: 20 млн на подготовку резервной процедуры; котировки отсутствуют")
    try:
        risk = Risk(risk_id="TEAM_SUPPLY", event="Задержка / недопоставка выбранного источника", source_id=source, start_year=years[0], end_year=years[1], delivery_share=share, delay_days=delay, price_multiplier=price, owner=owner, mitigation=mitigation, residual_delay_days=residual_delay, residual_delivery_share=residual_share, mitigation_cost_mln=mitigation_cost, mitigation_decision_date=decision_date, reaction_days=reaction_days, mitigation_cost_basis=cost_basis)
    except ValueError as exc:
        st.error(f"Некорректные параметры меры: {exc}")
        risk = None
    risk_sig = signature(plan, case, risk.model_dump_json()) if risk else "invalid"
    if st.button("Рассчитать риск", disabled=pending_edits or risk is None):
        try:
            affected, residual = assess_risk(case, plan, risk)
            st.session_state.risk_result = (risk_sig, affected, residual)
        except ValueError as exc:
            st.error(str(exc))
    rr = st.session_state.get("risk_result")
    if rr and rr[0] == risk_sig and not pending_edits:
        table([dict(Этап="При риске", **rr[1].summary()), dict(Этап="После меры", **rr[2].summary())])
        table(rr[1].payload["risk_register"])
        st.download_button("Экспорт риска XLSX", export_bytes(rr[1], "xlsx"), "risk.xlsx")
        st.download_button("Экспорт остаточного риска XLSX", export_bytes(rr[2], "xlsx"), "residual-risk.xlsx")
        with st.expander("Распределение последствий до и после меры"):
            table([dict(Этап=label, **row) for label, r in [("При риске", rr[1]), ("После меры", rr[2])] for row in r.payload["stakeholders"]])

with inputs_tab:
    st.markdown("**Редактирование исследовательской копии входных данных**")
    st.caption("Изменения спроса, мощностей, цен, lead time и договорных тарифов помечаются TEAM_ASSUMPTION. Контрольные CSV, обязательный стресс и hard limits сохраняются. Снимок копии сохраняется в JSON плана.")
    demand_columns = ["year", "base_total_t", "base_critical_t", "low_total_t", "high_total_t"]
    source_columns = ["source_id", "name", "capacity_t_per_year", "variable_cost_mln_per_t", "reservation_rate_mln_per_t_year_capacity", "take_or_pay_share", "lead_time_min_value", "lead_time_max_value", "lead_time_unit", "reliability_profile", "available_from_year", "notes"]
    demand_input = st.data_editor(pd.DataFrame(case.demand.values())[demand_columns], num_rows="dynamic", hide_index=True, key=f"case_demand_{version}")
    source_input = st.data_editor(pd.DataFrame(case.sources.values())[source_columns], num_rows="dynamic", hide_index=True, key=f"case_sources_{version}")
    rationale = st.text_input("Обоснование изменений входов", "Проверка чувствительности; не замена условий организатора")
    if st.button("Применить копию входных данных", disabled=pending_edits):
        try:
            revised = edit_case(case, clean_rows(demand_input), clean_rows(source_input), rationale)
            updated_plan = plan.model_copy(deep=True)
            updated_plan.case_snapshot = case_snapshot(revised)
            set_plan(updated_plan)
            st.rerun()
        except (ValueError, TypeError, KeyError) as exc:
            st.error(f"Данные не изменены: {exc}")
    st.download_button("Скачать входной снимок JSON", json.dumps(case_snapshot(case), ensure_ascii=False, indent=2), "case-snapshot.json")

with stakeholder_tab:
    st.caption("Денежная нагрузка: оператор — текущие расходы, финансирующая сторона — CAPEX. Поступления поставщиков не складываются с затратами повторно. 30% земной закупки отнесено запуску как TEAM_ASSUMPTION; выручка и денежный ущерб потребителей неизвестны. Веса не используются.")
    if st.button("Рассчитать интересы и roadmap", disabled=pending_edits):
        try:
            st.session_state.stakeholder_result = (signature(plan, case, scenario), evaluate(case, plan, scenario))
        except ValueError as exc:
            st.error(str(exc))
    sr = st.session_state.get("stakeholder_result")
    if sr and sr[0] == signature(plan, case, scenario) and not pending_edits:
        table(sr[1].payload["stakeholders"])
        table(sr[1].payload["roadmap"])
        st.download_button("Экспорт стейкхолдеров и roadmap", export_bytes(sr[1], "xlsx"), "stakeholders.xlsx")
    for title, path in [("Управленческая записка", ROOT/"docs"/"MANAGEMENT_REPORT.html"), ("Проверка критериев", ROOT/"docs"/"CRITERIA_AUDIT.md")]:
        if path.exists():
            st.download_button(title, path.read_bytes(), path.name)

with assumptions_tab:
    st.markdown("**Условия организатора**")
    table(list(case.demand.values()))
    table(list(case.sources.values()))
    st.markdown("**Принятые допущения**")
    st.markdown("""- Год содержит 365 дней; месяц равен 365/12 дня, задержка округляется вверх. По умолчанию используется верхняя граница lead time.
- До 2035 разрешены документированные заказы; стартовая партия поступает при инициализации и оплачивается в первом году.
- Стартовая партия учитывается как поступление ровно один раз. Для последующих лет резерв проверяется до новых поставок.
- Резерв — физический. Emergency не заменяет запас автоматически. Любое использование Emergency учитывается в лимите последовательных лет, включая активацию contingency.
- Earth-New: срок подготовки учитывается при вводе; последующие партии поставляются по расписанию годового договора.
- Заказы оплачиваются по году плановой поставки даже при сценарной недопоставке. Цены уже включают доставку.
- Потребление равномерное; критический спрос обслуживается первым. Переполнение вызывает явное отклонение избыточного поступления.
- Holding cost: среднее запаса после приемки и на конец дня. Ставка дисконтирования не применяется.
- BASE детерминирован; reliability сохраняется как metadata. Монте-Карло и вероятностные прогнозы не заявляются.
- Оптимизатор обслуживает 100% спроса, использует физический резерв и годовые даты инвестиций. Это ограниченная область поиска, а не доказательство лучшей стратегии при любых допущениях.""")
    if case.assumptions:
        st.warning("Исследовательская копия: " + " · ".join(case.assumptions))
    st.caption("Исходные CASE_INPUT не изменяются. Методология: docs/SCIENTIFIC_BASIS.md. Ограничения комплекта и отсутствие первичных DOCX/XLSX зафиксированы в docs/ERRATA_AND_PROVENANCE.md.")
