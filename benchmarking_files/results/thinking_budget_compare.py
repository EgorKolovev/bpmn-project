"""Compare thinking budgets (2048, 4000, 8000) on both customer benchmarks.

Calls Gemini API DIRECTLY (bypassing our /generate endpoint) so we can vary
thinkingBudget per request without container restart. Applies our
post-processing (validator, ensure_incoming_outgoing, ensure_lane_refs)
locally.

Runs 3 trials per (benchmark, budget) = 18 calls total.
Cost: ~$0.05 on flash-lite.

Produces a table comparing:
  * Lanes (count + names)
  * Gateways (exclusive + parallel)
  * Tasks
  * Named sequence flows
  * Elapsed time
  * Actual thinking tokens used (key signal: more budget → more thinking?)
  * Structural diffs across runs at same budget (stability)
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/app")

import xml.etree.ElementTree as ET
from app.validator import validate_bpmn_xml
from app.bpmn_fix import (
    ensure_incoming_outgoing,
    ensure_lane_refs,
    fix_missing_namespace_declarations,
    strip_bpmn_diagram,
)
from app.prompts import SYSTEM_PROMPT_GENERATE


BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

BENCHMARKS = {
    "sluzhebnaya_komandirovka": (
        "Процесс оформления служебной командировки сотрудника. "
        "Сотрудник согласовывает с руководителем ЦФО цель, даты и бюджет. "
        "Если командировка от другого ЦФО — сотрудник дополнительно "
        "согласовывает бюджет с СЕО того ЦФО. "
        "Сотрудник подаёт заявление через Nopaper. "
        "Бухгалтерия рассматривает заявление и проверяет корректность. "
        "Если заявление некорректно — возвращает сотруднику на доработку. "
        "Если всё корректно и бюджет в пределах лимита — сотрудник покупает "
        "билеты и бронирует гостиницу через Aviasales. "
        "Если бюджет превышает лимит — требуется согласование руководителя; "
        "при отказе сотрудник подбирает более дешёвые варианты. "
        "Сотрудник едет в командировку. "
        "После возвращения сотрудник собирает документы (билеты, посадочные, "
        "чеки) и передаёт их в бухгалтерию. "
        "Бухгалтерия проверяет документы и закрывает командировку. "
        "Если сотрудник опоздал на рейс по своей вине — он обязан возместить "
        "стоимость билета компании. "
        "Если опоздание не по вине сотрудника — компания оплачивает новый билет."
    ),
    "otpravka_dokumentov": (
        "Процесс отправки исходящих документов компании. "
        "Менеджер создаёт задачу на отправку документа в Service Desk, "
        "выбирая способ: Почта России, курьерская служба, ЭДО (Диадок или Nopaper). "
        "Офис-менеджер или Специалист по документообороту получает задачу и "
        "проверяет данные. "
        "Если данные некорректны — переводит задачу на уточнение менеджеру; "
        "если есть юридические вопросы — отправляет на проверку юристам. "
        "Если юристы не согласовали — задача закрывается. "
        "Если данные корректны — Специалист по документообороту подготавливает "
        "оригиналы документов. "
        "Если нужны подписи — собирает подписи всех подписантов. "
        "Далее документ отправляется через выбранный канал. "
        "После отправки документ регистрируется в исходящем реестре и "
        "передаётся на хранение."
    ),
}


API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")
MAX_OUT = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "16384"))


def _local(t: str) -> str:
    return t.split("}", 1)[-1] if "}" in t else t


def call_gemini(description: str, thinking_budget: int) -> dict:
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": f"Generate a BPMN 2.0 diagram for the following business process:\n\n{description}"}]}
        ],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT_GENERATE}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": MAX_OUT,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": thinking_budget},
        },
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
    )
    t = time.time()
    resp = urllib.request.urlopen(req, timeout=300)
    elapsed = time.time() - t
    data = json.loads(resp.read())
    candidate = data["candidates"][0]
    usage = data.get("usageMetadata", {})
    text = candidate.get("content", {}).get("parts", [{}])[0].get("text", "")
    return {
        "elapsed": elapsed,
        "finishReason": candidate.get("finishReason"),
        "usage": usage,
        "text": text,
    }


def repair_double_escape(text: str) -> str | None:
    if r'\"}' not in text and r'\",' not in text:
        return None
    try:
        return json.loads(f'"{text}"')
    except json.JSONDecodeError:
        return None


def parse_response(raw_text: str) -> dict | None:
    """Try to extract {bpmn_xml, session_name} from raw LLM text."""
    text = raw_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    repaired = repair_double_escape(text)
    if repaired:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass
    return None


def analyze_xml(xml: str) -> dict:
    """Structural metrics."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {"parse_error": True}
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        return {"no_process": True}
    counts = {"task": 0, "exclusiveGateway": 0, "parallelGateway": 0,
              "startEvent": 0, "endEvent": 0, "sequenceFlow": 0}
    lanes: list[str] = []
    named_flows = 0
    cond_flows = 0
    for elem in list(process):
        tag = _local(elem.tag)
        if tag == "laneSet":
            for lane in list(elem):
                if _local(lane.tag) == "lane":
                    lanes.append(lane.get("name", lane.get("id", "")))
        elif tag == "sequenceFlow":
            counts["sequenceFlow"] += 1
            if elem.get("name"):
                named_flows += 1
            for c in list(elem):
                if _local(c.tag) == "conditionExpression" and (c.text or "").strip():
                    cond_flows += 1
        elif tag in counts:
            counts[tag] += 1
    return {
        "counts": counts,
        "lanes": lanes,
        "named_flows": named_flows,
        "cond_flows": cond_flows,
        "bytes": len(xml),
    }


def run_one(bench_id: str, budget: int) -> dict:
    desc = BENCHMARKS[bench_id]
    raw = call_gemini(desc, budget)
    parsed = parse_response(raw["text"])
    if parsed is None:
        return {**raw, "parse_failed": True}
    bpmn_xml = parsed.get("bpmn_xml", "")
    # Apply same post-processing as our /generate endpoint
    bpmn_xml = strip_bpmn_diagram(bpmn_xml)
    bpmn_xml = fix_missing_namespace_declarations(bpmn_xml)
    err = validate_bpmn_xml(bpmn_xml)
    if err:
        return {**raw, "validation_error": err, "bpmn_xml": bpmn_xml}
    bpmn_xml = ensure_incoming_outgoing(bpmn_xml)
    bpmn_xml = ensure_lane_refs(bpmn_xml)
    metrics = analyze_xml(bpmn_xml)
    return {
        "elapsed": raw["elapsed"],
        "usage": raw["usage"],
        "session_name": parsed.get("session_name", ""),
        "metrics": metrics,
    }


def main():
    budgets = [2048, 4000, 8000]
    trials = 3
    results: dict = {b: {} for b in budgets}

    for budget in budgets:
        print(f"\n========== budget={budget} ==========")
        results[budget] = {}
        for bench_id in BENCHMARKS:
            results[budget][bench_id] = []
            for trial in range(trials):
                print(f"  [{bench_id} trial {trial+1}/{trials}]...", end="", flush=True)
                try:
                    r = run_one(bench_id, budget)
                except Exception as exc:
                    print(f" EXCEPTION: {exc}")
                    results[budget][bench_id].append({"error": str(exc)})
                    continue
                if "parse_failed" in r:
                    print(f" PARSE_FAIL")
                elif "validation_error" in r:
                    print(f" VALIDATION_FAIL: {r['validation_error'][:80]}")
                else:
                    m = r["metrics"]
                    usage = r["usage"]
                    print(
                        f" OK elapsed={r['elapsed']:.1f}s "
                        f"think_tokens={usage.get('thoughtsTokenCount', '?')} "
                        f"lanes={len(m['lanes'])} gw={m['counts']['exclusiveGateway']} "
                        f"tasks={m['counts']['task']} named_flows={m['named_flows']}"
                    )
                results[budget][bench_id].append(r)

    # Summary table
    print("\n\n" + "=" * 100)
    print("SUMMARY — per budget × benchmark (averaged over 3 trials)")
    print("=" * 100)
    for budget in budgets:
        print(f"\n-- budget={budget} --")
        for bench_id in BENCHMARKS:
            trials_data = [t for t in results[budget][bench_id] if "metrics" in t]
            if not trials_data:
                print(f"  [{bench_id}] all trials failed")
                continue
            lanes_list = [len(t["metrics"]["lanes"]) for t in trials_data]
            gw_list = [t["metrics"]["counts"]["exclusiveGateway"] for t in trials_data]
            task_list = [t["metrics"]["counts"]["task"] for t in trials_data]
            named_list = [t["metrics"]["named_flows"] for t in trials_data]
            think_list = [t["usage"].get("thoughtsTokenCount", 0) for t in trials_data]
            elapsed_list = [t["elapsed"] for t in trials_data]
            unique_lanes = {tuple(sorted(t["metrics"]["lanes"])) for t in trials_data}

            def _stats(xs):
                if not xs:
                    return "?"
                if len(set(xs)) == 1:
                    return f"{xs[0]}"
                return f"{min(xs)}-{max(xs)} (avg={sum(xs)/len(xs):.1f})"

            print(f"  [{bench_id}] ({len(trials_data)}/{trials} ok)")
            print(f"    lanes:       {_stats(lanes_list)}    {unique_lanes}")
            print(f"    gateways:    {_stats(gw_list)}")
            print(f"    tasks:       {_stats(task_list)}")
            print(f"    named_flows: {_stats(named_list)}")
            print(f"    think_tokens_used: {_stats(think_list)}  (budget={budget})")
            print(f"    elapsed (s): {_stats([round(e,1) for e in elapsed_list])}")

    # Save raw results
    out_path = "/tmp/thinking_compare_results.json"
    # Strip non-serializable metrics
    clean = {}
    for b, benches in results.items():
        clean[str(b)] = {}
        for bid, trials_data in benches.items():
            clean[str(b)][bid] = []
            for t in trials_data:
                t2 = {k: v for k, v in t.items() if k != "text"}
                clean[str(b)][bid].append(t2)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    print(f"\nRaw results saved to {out_path}")


if __name__ == "__main__":
    main()
