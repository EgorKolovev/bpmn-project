# Real-PDF benchmark (Iteration 3)

Ran the generator against the two real customer PDFs the client gave us
(both live in `benchmarking_files/`). These specs are 10–13 KB of
natural text each and contain reference BPMN diagrams embedded on the
last page — so we have a ground truth to compare against.

| PDF | chars | words | reference diagram |
|---|---|---|---|
| `143478330_*_komandirovka.pdf` | 13 244 | 1 917 | "Fly" — 4 lanes (Сотрудник / Руководитель ЦФО / Бухгалтерия / Отдел кадров), 5+ gateways, rework loop |
| `309532051_*_otpravka.pdf` | 9 521 | 1 191 | "processing of outgoing correspondence" — 3 lanes (Менеджер / Сотрудник документооборота / Офис-менеджер), 4+ gateways, "Уточнения" loop |

Score = 5 boolean checks:
1. `lanes_min` met
2. `lanes_ideal` met
3. `gw_ex_min` met
4. `tasks_min` met
5. cycle (back-edge) present

A trial must score ≥ 4/5 to count as passing.

## Results (3 trials per PDF, same prompt, default temp=0.2)

| config | PDF1 pass | PDF2 pass | PDF1 latency | PDF2 latency | notes |
|---|---|---|---|---|---|
| flash-lite-preview + think=2048 (old default) | **0/3** | 2/3 | 8 s | 7 s | flat on PDF1 — no cycle, only 2–3 gateways, happy-path only |
| flash-lite-preview + think=8192 | 1/3 | 0/1 | 14 s | 12 s | overthinks — burns 15 K thinking tokens, **truncates output at MAX_TOKENS** |
| gemini-2.5-flash + think=4096 | 3/3 ⭐ | 3/3 ⭐ | 60 s | 45 s | perfect quality, too slow for UX |
| **gemini-3-flash-preview + think=4096** ⭐ NEW DEFAULT | **3/3** | **3/3** | **27 s** | **32 s** | same quality as 2.5-flash, **2× faster** |

## Why flash-lite-preview failed on PDF1

Even with `thinkingBudget=8192`, flash-lite-preview kept flattening the
spec into a happy path:
- Missed the "согласовано / на доработку" rework loop on most trials.
- Collapsed "бюджет в лимите? / превышение? / руководитель согласовал
  расширение? / отказ → подбор дешевле" into a single gateway instead
  of the 3 distinct branches.
- Missed "опоздание по вине / не по вине" branching entirely.

These are business-critical flows — losing them makes the diagram
misleading for the actual process owner.

Raising the thinking budget to 8192 on flash-lite didn't fix reasoning —
it just made the model think longer without producing better structure,
sometimes consuming all 16 384 output tokens on thoughts alone and
truncating the XML mid-attribute.

## Why gemini-3-flash-preview is the right pick

- Catches every branching construct on both PDFs (5–12 gateways on
  PDF1, 6–8 on PDF2).
- Always produces a back-edge (rework cycle) when the spec implies one.
- Extracts 4–5 lanes consistently — preserves compound role labels
  like "Сотрудник документооборота / Офис-менеджер".
- 2× faster than 2.5-flash at identical structural quality.

Trade-off vs flash-lite-preview: ~3× latency (27–32 s vs 8 s) and
~2× cost per call. Acceptable for real specs — the old output was just
wrong on complex inputs.

## Config changes

- `GEMINI_MODEL` default: `gemini-3.1-flash-lite-preview` →
  **`gemini-3-flash-preview`**
- `GEMINI_THINKING_BUDGET` default: `2048` → **`4096`**
- `GEMINI_MAX_OUTPUT_TOKENS` default: `16384` → **`65536`** — observed
  that on 13 KB role-rich specs, gemini-3-flash-preview can emit 30 K+
  combined thinking + XML. 32 K was still occasionally truncating.
- `REQUEST_CHAR_LIMIT` default: `12000` → **`20000`** (Iteration 2
  fix, already in).

Prompt changes (`prompts.py`, SECTION 2):

- Added a **"NO SPURIOUS GATEWAYS"** rule. Gemini-3 is aggressive at
  extracting structure — without this rule it turns "HR verifies
  identity" into a Verified? gateway with a rework loop, even on
  trivial linear specs.
- The rule enumerates three explicit signals that DO require a
  gateway (contrasting outcomes / if-then-else / stated failure path)
  and says: in the absence of those signals, keep it linear. This
  gates the new model's enthusiasm without losing any real branches.

Override any of these via `.env` or docker-compose env vars; nothing is
hardcoded.

## Final verification

All three test corpora green after the config + prompt changes:

| Test | Trials | Result |
|---|---|---|
| `EN_SIMPLE_LINEAR` ("Employee onboarding: submit, verify, setup, assign") | 5 | 5/5 — 0 gateways, 0 cycles, 4 tasks |
| PDF1 командировка | 3 | 3/3 — avg 4.3 lanes, 6.0 gateways, cycle=True |
| PDF2 отправка | 3 | 3/3 — avg 4.3 lanes, 6.3 gateways, cycle=True |

## How to reproduce

```bash
# Inside the ml container, with the two PDFs copied into /tmp/
docker compose exec -T ml python /tmp/bench_pdfs.py
```

Script lives at `benchmarking_files/results/pdf_bench.py` (below).
