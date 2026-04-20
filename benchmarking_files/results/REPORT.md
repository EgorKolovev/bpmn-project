# Benchmark report — Iteration 2

Regression run of two customer-provided text specs through the updated
BPMN generator. Two LLM configurations were evaluated side-by-side:

- **flash-lite-preview** — `gemini-3.1-flash-lite-preview`
  (the model that was already configured before Iteration 2)
- **2.5-flash** — `gemini-2.5-flash` (thinking disabled)

**Default chosen: `flash-lite-preview`** — all E2E tests pass reliably
(87/87 on 3 consecutive runs); ~3× faster and ~1.7× cheaper than
2.5-flash. Customers needing maximum role-extraction fidelity on
long specs can switch via `GEMINI_MODEL=gemini-2.5-flash` in `.env`.

## Reproduce

```bash
# from inside the ml container
docker compose exec -T -e ML_URL=http://localhost:8001 \
  ml python /tmp/run_benchmark.py
```

## Benchmark 1 — Служебная командировка (~4800 word spec)

| Metric | flash-lite-preview | 2.5-flash |
|---|---|---|
| File size | 9097 bytes | 8637 bytes |
| Tasks | 13 | 14 |
| Exclusive gateways | 6 | 5 |
| Sequence flows | 25 | 23 |
| Named sequence flows | 12/25 | 9/23 |
| Flows with `<conditionExpression>` | 12 | 9 |
| **Lanes** | **2** (Сотрудник, Бухгалтерия) | **4** (Сотрудник, Руководитель ЦФО, СЕО другого ЦФО, Бухгалтерия) |
| Russian labels | ✓ all names ≥0.95 cyr ratio | ✓ same |
| Valid per validator | ✓ | ✓ |

**Qualitative observation:** flash-lite-preview collapsed "Руководитель
ЦФО" and "СЕО другого ЦФО" into Бухгалтерия/Сотрудник — the *decisions*
they represent are in the XML as gateways, but they're not broken out
into their own swimlanes. 2.5-flash correctly renders all four roles
as separate lanes.

## Benchmark 2 — Отправка документов (~2000 word spec)

| Metric | flash-lite-preview | 2.5-flash |
|---|---|---|
| File size | 5670 bytes | 6365 bytes |
| Tasks | 9 | 8 |
| Exclusive gateways | 2 | 4 |
| Sequence flows | 15 | 17 |
| Named sequence flows | 5/15 | 8/17 |
| Flows with `<conditionExpression>` | 5 | 8 |
| **Lanes** | **3** (Менеджер, Офис-менеджер/Специалист, Юристы) | **3** (same) |

On a smaller spec both models produce equivalent-quality output.

## E2E test suite stability (3 consecutive runs)

| Suite | flash-lite-preview | 2.5-flash |
|---|---|---|
| Level 2 i18n (14 tests) | 3×14 clean | 3×14 clean |
| Level 2 complex (9 tests) | 3×9 clean | 3×9 clean |
| Level 2 lanes (6 tests) | 3×6 clean | 3×6 clean |
| **Total Level 2** | **87/87** | **87/87** |
| Level 3 full-stack (5 tests) | 3×5 clean | 3×5 clean |

Before the `fix_missing_namespace_declarations` post-processing step
was added, flash-lite-preview occasionally emitted `xsi:type="..."`
without binding `xmlns:xsi` on the root (1 failure in 87 runs). The
post-processing step (see `ml/app/bpmn_fix.py`) now auto-injects
well-known namespace declarations (`xsi`, `bpmn`, `bpmndi`, `dc`, `di`)
when used but undeclared — 87/87 stable since.

## Performance / cost

| Metric | flash-lite-preview | 2.5-flash |
|---|---|---|
| Full Level 2 E2E runtime | ~80s | ~240s |
| List price input ($/1M tokens) | 0.25 | 0.30 |
| List price output ($/1M tokens) | 1.50 | 2.50 |
| Ballpark cost per BPMN generation | ~$0.002 | ~$0.004 |

## Recommendation

Use `flash-lite-preview` by default — passes all test criteria, 3× faster,
~40–50% cheaper. Switch to `2.5-flash` on long role-rich specs if fewer
lanes are being extracted than expected (see Benchmark 1 above).

Switch is a one-line `.env` change:
```
GEMINI_MODEL=gemini-2.5-flash
# or
POLZA_MODEL=google/gemini-2.5-flash  # when LLM_BACKEND=polza
```

## Known limitations

1. **Role coverage on long specs** — flash-lite-preview sometimes
   collapses multiple roles into a single lane on very long multi-role
   descriptions. Pro/2.5-flash do better but cost more.
2. **Unnamed intermediate flows** — non-conditional flows between two
   sequential tasks are unlabeled by design (standard BPMN).
3. **End-event lane placement** — LLM places endEvent in the lane of
   the last *action*, not the "owning" role.
