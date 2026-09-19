# Рассчитанные примеры

Этот каталог содержит результаты программного расчета участника. Они не входят в CASE_INPUT и не являются рекомендациями организатора.

Повторное построение:

```bash
.venv/bin/python tools/build_examples.py
```

- `comparison.csv`: Earth, Lunar, Diversified × BASE, mandatory stress, низкий и высокий спрос; одинаковый fingerprint.
- `optimizer.json`: фактические статусы решателя, стоимость, граница, зазор, время и fingerprint данных.
- `earth-*.zip`, `lunar-*.zip`, `diversified-*.zip`: CSV-таблицы полного результата, включая ежедневную трассу, snapshot, contracts, stakeholders, годовые проверки и метаданные. `unrestricted-*` — дополнительный технический пример, не четвертая архитектура.
- `earth-stress.xlsx`: пример экспорта в Excel с теми же числами.
- `risk-register.json`, `TEAM_*-before.zip`, `TEAM_*-residual.zip`: риски выбранной стратегии с причиной, владельцем, бюджетом/датой меры, рассчитанным снижением последствий и остатком. `risk-analysis.zip` соответствует TEAM_CORE_DELAY до меры.
- `sensitivity.csv`, `demand-threshold.json`: изменение спроса и граница нарушения годового резерва/сервиса.
- `research-extension.json`, `research-extension.zip`: отдельный расчет копии с Source-X и 2041, с явными допущениями.
- `selection.json`, `selected-BASE.json`, `selected-BASE.xlsx`: основание выбора и проверяемые числа выбранного плана.
- `*-response.zip`, `selected-stress-response.xlsx`: отдельная адаптация с сохранением исходных обязательств и сроками реакции.
- `sensitivity-multiparameter.csv`, `sensitivity-thresholds.csv`: четыре ключевых параметра, диапазоны, cost/service/inventory/shortage и границы.
- `stakeholders.csv`, `roadmap.csv`, `annual-constraints.csv`, `independent-controls.csv`: доказательные таблицы выбранной стратегии.
- `completion-checks.json`, `evidence-manifest.json`: машинные проверки и хеши артефактов; 10-страничный memo — в `docs/MANAGEMENT_REPORT.*`, одностраничное сравнение — в `docs/SCENARIO_SUMMARY.*`.

Планы находятся в [examples/plans](../examples/plans) и [examples/research_plans](../examples/research_plans). Контрольные стратегии используют один и тот же план для четырех сценариев, без скрытой перестройки заказов после шока.

`feasible` означает отсутствие **жестких** нарушений. Сервисные отклонения stress/low/high показаны отдельно; успешный BASE не доказывает устойчивость. `violations` учитывает и жесткие, и сервисные замечания. Для стратегии с минимальными затратами BASE высокая уязвимость при росте спроса ожидаема: оптимизатор не минимизирует стрессовый ущерб.

Числа следует читать из файлов результатов; время решателя зависит от машины. После изменения математической модели примеры нужно перестроить.
