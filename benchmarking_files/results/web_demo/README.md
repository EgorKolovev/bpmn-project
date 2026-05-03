# Web demo — командировка через `/generate`

Артефакты прогона PDF1 (Служебная командировка) через ml/`/generate`,
с **новым server-side lane-aware layouter'ом** (`ml/app/bpmn_layout.py`).

## Параметры прогона

- Модель: `gemini-3-flash-preview`
- Backend: direct Gemini (Polza dev-key был в дневном лимите)
- thinkingBudget: 4096
- Время: ~25–30 s
- Layout: **server-side** (lane shapes + node positions + edge waypoints
  baked в response XML)

## Структурные метрики

| Метрика | Значение |
|---|---|
| Lanes | 3 (Сотрудник / Руководитель CEO ЦФО / Бухгалтерия/кадры) |
| Tasks | 14–15 |
| Exclusive gateways | 5–8 |
| Sequence flows | 25–29 |
| Cycle (back-edge) | ✓ — на доработку |
| BPMNShape count | 25 (3 lanes + 22 flow nodes) |
| BPMNEdge count  | 25 |
| Waypoints | ~80 (4 per back-edge, 2–4 per forward edge) |

## Файлы

- `komandirovka.bpmn` — XML до layout (process + flow nodes + sequenceFlows).
- `komandirovka_layout.bpmn` — XML после server-side layouter — **с
  правильно расставленными lanes, нодами и waypoints**.
- `komandirovka.svg` — векторный рендер `bpmn-js` viewer'а.

## Как layouter работает

1. Парсит `<bpmn:laneSet>` и список `<bpmn:flowNodeRef>` для каждого lane.
2. Топологическая раскладка по колонкам (BFS от startEvents, back-edges
   обрабатываются — снова не считают глубину).
3. Каждой lane даётся горизонтальная полоса; нода размещается в
   колонке × своей lane. Несколько нод в одной (lane × column) —
   стэкуются вертикально.
4. Edges:
   * Forward, same row → прямая линия (2 waypoints).
   * Forward, cross-lane → elbow (4 waypoints).
   * Back-edge → **U-bend под ряд** (4 waypoints — обходит ноды без
     overlap-а).
5. Эмитит полный `<bpmndi:BPMNDiagram>` со всеми BPMNShape (включая
   `isHorizontal="true"` для lanes), BPMNEdges и waypoints.

## Что осталось как trade-off

- Простое orthogonal routing — несколько edges с одинаковым target Y
  могут перекрываться. Бизнес-смысл сохраняется, но визуально
  встречается слегка зашумлённое место.
- Sub-rows внутри lane делятся равномерно по высоте — не идеально
  оптимизирует пространство, но всегда даёт читаемый результат.
- Front-end fallback (`bpmn-auto-layout` JS) остаётся в качестве
  defence-in-depth: если ml вернул XML без BPMNDiagram (старая
  версия / кастомный paste), фронт всё ещё отрисует.
