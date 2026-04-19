# Benchmark report — Iteration 2

Regression run of two customer-provided text specs through the updated
BPMN generator (Gemini 2.5 Flash + new prompts + post-processing).

## Reproduce

```bash
# from inside the ml container
docker compose exec -T -e ML_URL=http://localhost:8001 \
  ml python /tmp/run_benchmark.py
# outputs: {id}.bpmn + {id}.meta.json
```

Source specs live in `../*.pdf` (PDF text extracted and condensed into a
single paragraph fed to `/generate` — see `run_benchmark.py`
`BENCHMARKS` list for the exact prompt).

## Benchmark 1 — Служебная командировка

**Source:** `143478330_…pdf` (9 pages, ≈4800 words of corporate instructions)

**Generated session name:** *"Оформление служебной командировки"*

### Structural summary

| Metric | Value |
|---|---|
| File size | 8637 bytes |
| Start events | 1 |
| End events | 1 |
| Tasks | 14 |
| Exclusive gateways | **5** |
| Sequence flows | 23 |
| Named sequence flows | 9/23 |
| Flows with `<conditionExpression>` | 9 |
| Lanes | **4** |

### Lanes detected

| Lane | Nodes |
|---|---|
| Сотрудник | 12 (Start_1, Task_1, Gateway_1, Task_2, Task_3, Task_4, Task_6, Task_7, Task_8, Gateway_4, Gateway_5, Task_Rework_Budget) |
| Руководитель ЦФО | 1 |
| СЕО другого ЦФО | 1 |
| Бухгалтерия | 7 |

### Decisions captured (matching spec)

| Decision | Gateway labels present |
|---|---|
| Командировка от своего / другого ЦФО | ✓ |
| Бухгалтерия: заявление корректно / некорректно (rework) | ✓ |
| Бюджет в пределах лимита / превышает лимит | ✓ |
| Руководитель согласовал / отказал при превышении лимита | ✓ |
| Опоздание на рейс: по своей / не по своей вине | ✓ |

### Russian labels sample

- Согласовать цель, даты, бюджет с руководителем ЦФО
- Согласовать бюджет с СЕО другого ЦФО
- Подать заявление через Nopaper
- Рассмотреть заявление и проверить корректность
- Доработать заявление
- Купить билеты и забронировать гостиницу через Aviasales
- Поехать в командировку
- Собрать документы (билеты, посадочные, чеки)
- Проверить документы и закрыть командировку
- Возместить стоимость билета компании
- Оплатить новый билет
- Подобрать более дешёвые варианты

All 14 task names in Russian; cyrillic ratio ≥0.95 in every name.

### Comparison to reference diagrams "Fly" / "Fly for CEO and heads"

The reference BPMN XMLs were not attached in the benchmark package
(PDFs only), so side-by-side is structural:

- ✓ All four roles mentioned in the PDF appear as lanes (matches the
  reference visual: Сотрудник / Руководитель ЦФО / СЕО / Бухгалтерия).
- ✓ Key decisions (fault/no-fault late arrival, budget limit, rework
  loop, from-other-CFO path) are materialised as gateways.
- ✓ Labels in Russian throughout.
- ⚠ Start / End are both in the "Сотрудник" lane. In the reference
  "Fly" visual the process starts in Сотрудник and ends in Бухгалтерия
  after document handoff — our output ends in the same lane as the
  documents are handed off. `ensure_lane_refs` keeps these in whichever
  lane the LLM chose; acceptable.
- ⚠ 9/23 named flows. Non-conditional flows (e.g. end-of-linear-segment
  transitions) are unlabeled, which is standard BPMN.

## Benchmark 2 — Отправка документов/писем

**Source:** `309532051_…pdf` (6 pages, ≈2000 words)

**Generated session name:** *"Отправка исходящих документов компании"*

### Structural summary

| Metric | Value |
|---|---|
| File size | 6365 bytes |
| Start events | 1 |
| End events | 2 *(one is the "rejected by legal" terminal)* |
| Tasks | 8 |
| Exclusive gateways | **4** |
| Sequence flows | 17 |
| Named sequence flows | 8/17 |
| Flows with `<conditionExpression>` | 8 |
| Lanes | **3** |

### Lanes detected

| Lane | Nodes |
|---|---|
| Менеджер | 4 (Start_1, Task_1, Task_3, End_2) |
| Офис-менеджер / Специалист по документообороту | 9 (Task_2, Gateway_1, Gateway_2, Task_5, Gateway_3, Task_6, Task_7, Task_8, End_1) |
| Юрист | 2 |

Matches the RACI table at the top of the source PDF.

### Russian labels sample

- Создать задачу на отправку
- Проверить данные
- Уточнить данные
- Проверка юристами
- Подготовить оригиналы документов
- Собрать подписи
- Отправить документ
- Зарегистрировать и передать на хранение

## Cross-task acceptance criteria

| Task | Criterion | Status |
|---|---|---|
| 1 | Russian labels on every name | ✓ (≥0.95 cyrillic ratio across all 22 task/lane names) |
| 2 | Non-linear: ≥1 exclusiveGateway per complex process | ✓ (5 and 4) |
| 2 | At least one flow labeled (name or conditionExpression) per diverging gateway | ✓ (validator would have rejected otherwise) |
| 2 | Rework loop materialised as cycle when spec implies it | ✓ (доработка заявления in Benchmark 1) |
| 3 | ≥3 lanes when spec mentions ≥3 roles | ✓ (4 and 3) |
| 3 | Every flow node referenced in exactly one `<flowNodeRef>` | ✓ (`ensure_lane_refs` guarantees this) |
| — | XML valid per our validator | ✓ (validate_bpmn_xml returned None) |
| — | Loads in bpmn-js NavigatedViewer | ✓ (verified via frontend) |

## Known limitations

1. **End-event lane placement** — the LLM puts endEvent in the lane of
   the last *action*, not necessarily the "owning" role. Acceptable
   BPMN but may differ from a human-drawn reference.
2. **Non-conditional flow labels** — unnamed flows between two tasks
   in the same pass are intentional (BPMN best practice is to label
   only branching flows).
3. **Gateway names** — some gateways have `name` set, others don't. Our
   validator only requires at least one OUTGOING flow per gateway to be
   labeled, not the gateway itself.

## Recommendations for further iteration

- If the reference XMLs become available — diff them structurally
  (node count, gateway count, lane membership, edge cross-lane count).
- Consider a "fidelity score" metric: Jaccard on {lane_name} and
  {gateway_label} sets vs reference.
- The complex-process E2E suite covers these structural invariants and
  is stable at 29/29 on gemini-2.5-flash.
