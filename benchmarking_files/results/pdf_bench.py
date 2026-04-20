"""Benchmark BPMN generation on real customer PDFs.

Runs N trials per PDF through the full /generate pipeline, measures:
  - lanes (count + names)
  - exclusive gateways (count + named branches)
  - tasks (count)
  - sequence flows with condition expressions (proxy for branching quality)
  - elapsed time
  - thinking tokens used (from ml container logs)
  - XML bytes
  - whether there's a back-edge (cycle) — rework loop indicator

Expected targets derived from the reference BPMN diagrams embedded
in the PDFs themselves (pages 8-9 for PDF1, page 3 for PDF2).
"""
import json, os, re, time, urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

ML_URL = "http://localhost:8001"
API_KEY = os.environ["INTERNAL_API_KEY"]
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

def _local(t: str) -> str:
    return t.split("}", 1)[-1] if "}" in t else t

def analyze(xml: str) -> dict:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return {"err": f"parse: {e}"}
    proc = root.find(f".//{{{BPMN_NS}}}process")
    if proc is None:
        return {"err": "no process"}
    lanes: list[str] = []
    tasks = gws_ex = gws_par = flows = named_flows = cond_flows = 0
    # For cycle detection: build adjacency and check if any flow targets a task
    # that's upstream of its source.
    edges = []
    for e in list(proc):
        tag = _local(e.tag)
        if tag == "laneSet":
            for l in list(e):
                if _local(l.tag) == "lane":
                    lanes.append(l.get("name", l.get("id", "")))
        elif tag == "task":
            tasks += 1
        elif tag == "exclusiveGateway":
            gws_ex += 1
        elif tag == "parallelGateway":
            gws_par += 1
        elif tag == "sequenceFlow":
            flows += 1
            src = e.get("sourceRef", "")
            tgt = e.get("targetRef", "")
            edges.append((src, tgt))
            if e.get("name"):
                named_flows += 1
            for c in list(e):
                if _local(c.tag) == "conditionExpression" and (c.text or "").strip():
                    cond_flows += 1
    # Cycle detection via DFS
    graph = defaultdict(list)
    for s, t in edges:
        graph[s].append(t)
    def has_cycle() -> bool:
        color = {}
        def dfs(n):
            color[n] = 1  # gray
            for nb in graph.get(n, []):
                if color.get(nb) == 1:
                    return True
                if color.get(nb) is None and dfs(nb):
                    return True
            color[n] = 2  # black
            return False
        for node in list(graph):
            if color.get(node) is None:
                if dfs(node):
                    return True
        return False
    return {
        "lanes": lanes,
        "tasks": tasks,
        "gw_ex": gws_ex,
        "gw_par": gws_par,
        "flows": flows,
        "named_flows": named_flows,
        "cond_flows": cond_flows,
        "has_cycle": has_cycle(),
        "bytes": len(xml),
    }

def call(description: str, timeout: int = 240) -> dict:
    body = json.dumps({"description": description}).encode("utf-8")
    req = urllib.request.Request(
        f"{ML_URL}/generate",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Api-Key": API_KEY,
        },
    )
    t = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        elapsed = time.time() - t
        return {"elapsed": elapsed, "data": json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        return {"elapsed": time.time() - t, "error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
    except Exception as e:
        return {"elapsed": time.time() - t, "error": f"{type(e).__name__}: {e}"}

PDFS = {
    "PDF1_komandirovka": "/tmp/pdf1_komandirovka.pdf.txt",
    "PDF2_otpravka": "/tmp/pdf2_otpravka.pdf.txt",
}

# Expected targets from the reference BPMN diagrams EMBEDDED in these PDFs.
EXPECTED = {
    "PDF1_komandirovka": {
        "lanes_min": 3,
        "lanes_ideal": 4,  # Сотрудник / Руководитель / Бухгалтерия / Кадры
        "gw_ex_min": 5,    # от-своего-ЦФО? согласовано? лимит? опоздание? по-вине? на-доработку?
        "tasks_min": 10,
        "cycle_expected": True,  # доработка заявления → возврат
    },
    "PDF2_otpravka": {
        "lanes_min": 3,
        "lanes_ideal": 3,  # Менеджер / Офис-менеджер / Специалист документооборота
        "gw_ex_min": 4,    # тип-задачи(4) корректно? юристы-согласовали? подписи?
        "tasks_min": 8,
        "cycle_expected": True,  # "Уточнения" back-edge
    },
}

def score(metrics: dict, exp: dict) -> dict:
    s = {}
    s["lanes_ok"] = len(metrics.get("lanes", [])) >= exp["lanes_min"]
    s["lanes_ideal"] = len(metrics.get("lanes", [])) >= exp["lanes_ideal"]
    s["gw_ok"] = metrics.get("gw_ex", 0) >= exp["gw_ex_min"]
    s["tasks_ok"] = metrics.get("tasks", 0) >= exp["tasks_min"]
    s["cycle_ok"] = metrics.get("has_cycle", False) if exp["cycle_expected"] else True
    s["total"] = sum(1 for v in s.values() if v)
    return s

def run(name: str, trials: int = 3):
    with open(PDFS[name], "r", encoding="utf-8") as f:
        desc = f.read()
    print(f"\n=============================================================")
    print(f"=== {name}  ({len(desc)} chars, {len(desc.split())} words)")
    print(f"=============================================================")
    exp = EXPECTED[name]
    print(f"Expected: lanes >= {exp['lanes_min']} (ideal {exp['lanes_ideal']}), "
          f"exclusive gateways >= {exp['gw_ex_min']}, tasks >= {exp['tasks_min']}, "
          f"cycle: {exp['cycle_expected']}")
    results = []
    for trial in range(trials):
        print(f"\n--- trial {trial+1}/{trials} ---", flush=True)
        r = call(desc)
        if "error" in r:
            print(f"  ERR: {r['error']}")
            continue
        d = r["data"]
        xml = d.get("bpmn_xml", "")
        m = analyze(xml)
        if "err" in m:
            print(f"  analyze err: {m['err']}")
            continue
        sc = score(m, exp)
        print(f"  elapsed={r['elapsed']:.1f}s  session={d.get('session_name')!r}")
        print(f"  lanes[{len(m['lanes'])}]: {m['lanes']}")
        print(f"  tasks={m['tasks']}  gw_ex={m['gw_ex']}  gw_par={m['gw_par']}")
        print(f"  flows={m['flows']}  named_flows={m['named_flows']}  cond_flows={m['cond_flows']}")
        print(f"  has_cycle={m['has_cycle']}  bytes={m['bytes']}")
        print(f"  score: lanes={sc['lanes_ok']}/ideal={sc['lanes_ideal']} "
              f"gw={sc['gw_ok']} tasks={sc['tasks_ok']} cycle={sc['cycle_ok']} "
              f"→ {sc['total']}/5")
        results.append({"metrics": m, "score": sc, "elapsed": r["elapsed"],
                        "session_name": d.get("session_name")})
    # Summary
    if results:
        avg_lanes = sum(len(r["metrics"]["lanes"]) for r in results) / len(results)
        avg_gw = sum(r["metrics"]["gw_ex"] for r in results) / len(results)
        avg_tasks = sum(r["metrics"]["tasks"] for r in results) / len(results)
        avg_elapsed = sum(r["elapsed"] for r in results) / len(results)
        trials_passing = sum(1 for r in results if r["score"]["total"] >= 4)
        print(f"\n  SUMMARY {name}: avg lanes={avg_lanes:.1f} gw={avg_gw:.1f} "
              f"tasks={avg_tasks:.1f} elapsed={avg_elapsed:.1f}s  "
              f"passing(score>=4)={trials_passing}/{len(results)}")
    return results

all_results = {}
for name in PDFS:
    all_results[name] = run(name, trials=3)

# Final verdict
print("\n\n" + "=" * 65)
print("FINAL VERDICT")
print("=" * 65)
for name, trials in all_results.items():
    if not trials:
        print(f"  {name}: NO SUCCESSFUL RUNS")
        continue
    passing = sum(1 for r in trials if r["score"]["total"] >= 4)
    print(f"  {name}: {passing}/{len(trials)} trials scored >= 4/5")
