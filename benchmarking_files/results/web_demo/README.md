# Web demo — командировка через Polza/Gemini

Артефакты прогона "сложного сценария" (PDF1 — Служебная командировка)
через `/generate` endpoint, отрисовка в `bpmn-js` viewer (тот же
рендерер, что использует наш фронтенд).

## Параметры прогона

- Модель: `gemini-3-flash-preview`
- Backend: direct Gemini API (Polza dev-key упёрся в дневной cap)
- thinkingBudget: 4096
- Время: 27.1 s
- Стоимость: ~$0.05 (по нашему локальному гарду)

## Структурные метрики

| Метрика | Значение |
|---|---|
| Lanes | 3 (Сотрудник / Руководитель CEO ЦФО / Бухгалтерия) |
| Tasks | 15 |
| Exclusive gateways | 8 |
| Sequence flows | 29 |
| Cycle (back-edge) | ✓ — на доработку |
| XML bytes | 9 429 |

## Файлы

- `komandirovka.bpmn` — сырой XML, как его вернул `/generate`.
- `komandirovka_layout.bpmn` — после прогона `bpmn-auto-layout`
  (добавляет `bpmndi:BPMNDiagram` с координатами шейпов).
- `komandirovka.svg` — готовый векторный рендер `bpmn-js` viewer'а.

## Замечание про lane-layout

`bpmn-auto-layout` v0.4 не выкладывает `<bpmn:laneSet>` как
горизонтальные дорожки — все 15 tasks в одну линию, поэтому SVG
получается широким (3 533 × 370 единиц). Лейнсет в XML присутствует и
корректен — проблема чисто в layout-движке. На реальном фронтенде это
выглядит так же, потому что мы используем тот же `bpmn-auto-layout`.

Возможные направления чтобы это починить (не делал):

1. Перейти на `bpmnlint` + `bpmn-js-task-priority-color` или
   `dagre`-based external layouter, который умеет swimlanes.
2. Передавать модели подсказку «верстай горизонтальные lanes» —
   некоторые BPMN-инструменты делают так.
3. Свой layouter поверх готового XML на Python/Node — задача
   отдельной итерации.
