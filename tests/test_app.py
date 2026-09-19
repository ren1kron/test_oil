from streamlit.testing.v1 import AppTest

from fuel_planner.data import ROOT, load_case
from fuel_planner.models import Plan


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_app_calculate_switch_and_compare():
    app = AppTest.from_file(str(ROOT/"app.py"), default_timeout=30).run()
    assert not app.exception
    button(app, "Рассчитать").click().run()
    assert not app.exception
    assert len(app.metric) == 4
    assert app.session_state["result"].payload["scenario_id"] == "BASE"
    next(s for s in app.selectbox if s.label == "Сценарий").select("MANDATORY_STRESS").run()
    assert any("устарел" in w.value for w in app.warning)
    button(app, "Рассчитать").click().run()
    assert app.session_state["result"].payload["scenario_id"] == "MANDATORY_STRESS"
    button(app, "Сравнить сценарии").click().run()
    assert len(app.session_state["comparison"][1]) == 4
    assert not app.exception


def test_app_edits_and_optimization():
    app = AppTest.from_file(str(ROOT/"app.py"), default_timeout=30).run()
    next(t for t in app.text_input if t.label == "Идентификатор плана").set_value("edited").run()
    assert button(app, "Рассчитать").disabled
    button(app, "Применить изменения").click().run()
    assert app.session_state["plan"]["plan_id"] == "edited"
    for key, value in [("opt_EARTH_NEW", 2035), ("opt_ZBO", 2036), ("opt_LUNAR_ISRU", "never")]:
        app.selectbox(key=key).select(value)
    button(app, "Найти план").click().run()
    assert not app.exception
    assert app.session_state["optimization"].validated
    button(app, "Использовать найденный план").click().run()
    assert app.session_state["plan"]["plan_id"] == "optimized"
