"""Benchmark runner — feed condensed customer specs through our /generate
endpoint and save the resulting BPMN XML for side-by-side review.

Usage (from host):
    python3 benchmarking_files/results/run_benchmark.py

Requires the ML service to be running. Auth via INTERNAL_API_KEY env var
(reads from project .env if present).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH_DIR = HERE.parent
ENV_PATH = BENCH_DIR.parent / ".env"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def call_generate(url: str, api_key: str, description: str) -> dict:
    body = json.dumps({"description": description}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Internal-Api-Key"] = api_key
    req = urllib.request.Request(url + "/generate", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


BENCHMARKS = [
    {
        "id": "sluzhebnaya_komandirovka",
        "title": "Служебная командировка (Fly)",
        "description": (
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
    },
    {
        "id": "otpravka_dokumentov",
        "title": "Отправка документов/писем",
        "description": (
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
    },
]


def main() -> int:
    env = load_env(ENV_PATH)
    url = os.environ.get("ML_URL", "http://localhost:8001")
    api_key = os.environ.get("INTERNAL_API_KEY") or env.get("INTERNAL_API_KEY", "")

    for bench in BENCHMARKS:
        print(f"[{bench['id']}] generating…", flush=True)
        try:
            result = call_generate(url, api_key, bench["description"])
        except Exception as exc:
            print(f"  FAILED: {exc}")
            continue

        xml = result.get("bpmn_xml", "")
        name = result.get("session_name", "")
        out_xml = HERE / f"{bench['id']}.bpmn"
        out_xml.write_text(xml, encoding="utf-8")
        out_meta = HERE / f"{bench['id']}.meta.json"
        out_meta.write_text(
            json.dumps(
                {
                    "id": bench["id"],
                    "title": bench["title"],
                    "description": bench["description"],
                    "session_name": name,
                    "xml_bytes": len(xml),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  ✓ wrote {out_xml.name} ({len(xml)} bytes, session_name={name!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
