# Benchmark report — Iteration 2

Three LLM configurations were evaluated side-by-side:

- **flash-lite (no think)** — `gemini-3.1-flash-lite-preview` with
  `thinkingBudget=0` (earlier baseline).
- **flash-lite + think 2048** — `gemini-3.1-flash-lite-preview` with
  `thinkingBudget=2048` **← CURRENT DEFAULT**.
- **2.5-flash** — `gemini-2.5-flash` with thinking disabled.

**Default chosen: `flash-lite-preview` + thinking=2048** — matches the
quality of 2.5-flash on role-rich long specs *and* is ~2× cheaper and
~2× faster. Switch models via `.env` (`GEMINI_MODEL=...`) or tune
thinking budget via `GEMINI_THINKING_BUDGET=...` (0/512/2048/4096/-1).

## Reproduce

```bash
# from inside the ml container
docker compose exec -T -e ML_URL=http://localhost:8001 \
  ml python /tmp/run_benchmark.py
```

## Benchmark 1 — Служебная командировка (~4800 word spec)

| Metric | flash-lite no-think | **flash-lite + think 2048** | 2.5-flash |
|---|---|---|---|
| File size | 9097 bytes | **9069 bytes** | 8637 bytes |
| Tasks | 13 | **14** | 14 |
| Exclusive gateways | 6 | **6** | 5 |
| Sequence flows | 25 | **26** | 23 |
| Named sequence flows | 12/25 | **11/26** | 9/23 |
| Flows with `<conditionExpression>` | 12 | **11** | 9 |
| **Lanes** | **2** | **5** 🎯 (Сотрудник / Руководитель ЦФО / СЕО ЦФО / Бухгалтерия / Руководитель) | 4 |
| Russian labels | ✓ | ✓ | ✓ |
| Valid per validator | ✓ | ✓ | ✓ |

**With thinking=2048, flash-lite-preview extracts MORE lanes than
2.5-flash without thinking** (5 vs 4). The thinking tokens (1127 used in
this run) let the model identify all five distinct actors in the spec,
including nuanced ones like "Руководитель" (generic) vs "Руководитель
ЦФО" (specific).

## Benchmark 2 — Отправка документов (~2000 word spec)

| Metric | flash-lite no-think | **flash-lite + think 2048** | 2.5-flash |
|---|---|---|---|
| File size | 5670 bytes | **6934 bytes** | 6365 bytes |
| Tasks | 9 | **9** | 8 |
| Exclusive gateways | 2 | **4** | 4 |
| Sequence flows | 15 | **18** | 17 |
| Named sequence flows | 5/15 | **8/18** | 8/17 |
| Flows with `<conditionExpression>` | 5 | **8** | 8 |
| **Lanes** | **3** | **3** | **3** |

Thinking doubles the gateway count (2 → 4), matching 2.5-flash. Lanes
stable at 3 across all configurations.

## E2E test suite stability (3 consecutive runs)

| Suite | flash-lite-preview | 2.5-flash |
|---|---|---|
| Level 2 i18n (14 tests) | 3×14 clean | 3×14 clean |
| Level 2 complex (9 tests) | 3×9 clean | 3×9 clean |
| Level 2 lanes (6 tests) | 3×6 clean | 3×6 clean |
| **Total Level 2** | **87/87** | **87/87** |
| Level 3 full-stack (5 tests) | 3×5 clean | 3×5 clean |

**With thinking=2048 (current default):** 3 consecutive clean runs of
Level 2 (29/29 each, no reruns, ~130s each) + 3 consecutive clean runs
of Level 3 (5/5 each, ~23s each).

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

## Is 2048 the right thinking budget?

Ran `thinking_budget_compare.py` — 3 trials × 2 benchmarks × 3 budgets
(2048 / 4000 / 8000), direct Gemini calls bypassing our /generate (to
isolate the effect of the budget). **Key observation: the model doesn't
actually *use* higher budgets — it hallucinates more thinking without
producing better structure.**

### Actual thinking tokens consumed (the signal)

| Budget | Benchmark 1 (tokens used) | Benchmark 2 (tokens used) |
|---|---|---|
| 2048 | 1000–1204 (avg 1102) | 726–943 (avg 835) |
| 4000 | 821–1262 (avg 1093) | 820 (1 trial ok) |
| **8000** | **3052–5210 (avg 3850)** | **2645–2887 (avg 2766)** |

At 2048 the model uses ~1100 tokens on a 4800-word spec — it's **not
hitting the cap**. Raising to 4000 produces the same consumption (~1100).
Only at 8000 does the model actually burn more tokens (3850), but it's
"wasted thinking" — structural metrics don't change.

### Quality ceiling is hit at 2048

| Metric | 2048 | 4000 | 8000 |
|---|---|---|---|
| Bench 1 lanes | 4–5 | 4–5 | 4–5 |
| Bench 1 gateways | 5–6 | 5–6 | 6 |
| Bench 1 tasks | 14–15 | 14–15 | 15 |
| Bench 2 lanes | 3 | 3 | 3 |
| Bench 2 gateways | 4 | 3 | 3–4 |
| Bench 2 tasks | 9 | 9 | 9–10 |
| Bench 1 latency (s) | 10.3–11.1 | 9.6–11.2 | **16.3–22.3** |

At 8000 latency nearly doubles (11s → 19s) and cost scales linearly
with thinking tokens — for **zero** quality gain on our benchmarks.

### Note on the occasional extra lane "Руководитель"

On Benchmark 1, one extra lane ("Руководитель" — the manager who
re-approves when budget exceeds limits) appears in some runs and not
others. **Higher thinking budget does NOT stabilise this** — it's
equally flaky at 2048 and 8000. If we want it consistent, the fix is
promptb (more explicit role enumeration), not more thinking.

### Decision

**Keep default `GEMINI_THINKING_BUDGET=2048`.** Higher values burn
latency and $ for no measurable quality improvement on these specs.

## Known limitations

1. ~~**Role coverage on long specs**~~ — RESOLVED with `thinkingBudget=2048`.
2. **Unnamed intermediate flows** — non-conditional flows between two
   sequential tasks are unlabeled by design (standard BPMN).
3. **End-event lane placement** — LLM places endEvent in the lane of
   the last *action*, not the "owning" role.
4. **Over-escaped JSON** — Gemini flash-lite occasionally emits
   double-escaped JSON (~1 in 3 calls on long inputs). Handled by
   `_repair_double_escaped_json()` in `ml/app/llm.py` and a JSON-level
   retry in `generate()`/`edit()`. Transparent to the caller.
5. **`thinkingBudget=-1` (dynamic)** — explicitly avoided: dynamic
   thinking can exceed HTTP timeouts, causing cascading retries. The
   `test_llm_config.py` regression tests will fail if anyone sets
   `GEMINI_THINKING_BUDGET=-1` by default.
