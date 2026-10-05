"""
Имитация сервера Ollama для тестов и показа интерфейса без модели. Протокол тот же, что у настоящей
Ollama (POST /api/chat, ответ потоком NDJSON), поэтому проверяется весь путь
LangChain → ChatOllama → HTTP → разбор структурированного ответа → проверка цитат.

Ответы собираются простыми правилами по тексту запроса. Это НЕ ответы Qwen: имитация нужна, чтобы
проверить приложение там, где модели нет. Качество ответов Qwen проверяйте `manage.py check_llm` у себя.

    python tests/fake_ollama.py --port 11500
    LLM_PROVIDER=ollama LLM_MODEL=qwen-fake LLM_BASE_URL=http://127.0.0.1:11500 python manage.py runserver
"""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Шаблон в тексте → код находки ("" — изменение без кода), вид фрагмента, маршрут, специальность, совет.
RULES = [
    (r"[Кк]онкремент\w*[^.\n]*|холецистолитиаз\w*", "gallstones", "finding", "gb_surgical", "surgeon",
     "Направить к хирургу по хирургическому маршруту желчного пузыря"),
    (r"при натуживании[^.\n]*клетчатка", "hernia", "finding", "surgery_general", "surgeon",
     "Направить на консультацию хирурга: по описанию похоже на пупочную грыжу, в заключение не вынесено"),
    (r"лимфатическ\w* узел[^.\n]*", "", "abnormal", "", "", ""),
    (r"[Пп]олип\w* эндометри\w*", "endometrial_polyp", "finding", "gyn_surgical", "gyn_surgeon",
     "Направить к оперирующему гинекологу"),
]


def _matches(text: str):
    for pattern, code, kind, route, specialty, advice in RULES:
        for m in re.finditer(pattern, text):
            yield m.group(0).strip(), code, kind, route, specialty, advice


def answer(schema_title: str, messages: list[dict]) -> dict | str:
    human = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    if schema_title == "ExtractionPayload":
        text = human.split("<<<", 1)[-1].rsplit(">>>", 1)[0]
        findings = [{"code": code, "label": quote, "evidence_quote": quote, "confidence": 0.8}
                    for quote, code, *_ in _matches(text) if code]
        return {"findings": findings, "recommendations": []}
    if schema_title == "SegmentLabeling":
        labels = []
        for line in human.splitlines():
            seg = re.match(r"^\[(\d+)\] \((\w+)\) (.*)$", line)
            if not seg:
                continue
            found = list(_matches(seg.group(3)))
            if found:
                quote, code, kind, *_ = found[0]
                labels.append({"segment_id": int(seg.group(1)), "kind": kind, "highlight": True,
                               "finding_codes": [code] if code else [], "evidence_quote": quote})
        return {"labels": labels}
    if schema_title == "AdviceList":
        advice, seen = [], set()
        for line in human.splitlines():
            trig = re.match(r"^- \S+ [^:]+: «(.+?)» \((.*)\)(.*)$", line)
            if not trig:
                continue
            for _quote, code, _kind, route, specialty, text in _matches(trig.group(1)):
                if code and text and (route, trig.group(1)) not in seen:
                    seen.add((route, trig.group(1)))
                    only_ai = "только ИИ" in trig.group(3)
                    advice.append({"text": text, "evidence_quote": trig.group(1), "route_code": route,
                                   "specialty_code": specialty, "executor": "Координатор хирургического маршрута",
                                   "confidence": 0.6 if only_ai else 0.85,
                                   "rationale": f"В протоколе: «{trig.group(1)}»" + (
                                       "; словарь этого не нашёл, стоит уточнить у врача УЗИ" if only_ai else "")})
                    break
        return {"advice": advice}
    return "готов"


class FakeOllama:
    """Сервер в отдельном потоке. requests — все запросы /api/chat (для проверок в тестах)."""

    def __init__(self, port: int = 0, delay: float = 0.0) -> None:
        self.requests: list[dict] = []
        self.delay = delay
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # тихо: тесты читают только свои сообщения
                pass

            def do_GET(self):
                self._json({"models": [{"name": "qwen-fake", "model": "qwen-fake"}]} if self.path == "/api/tags" else {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path != "/api/chat":
                    self._json({})
                    return
                server.requests.append(body)
                if server.delay:
                    time.sleep(server.delay)
                fmt = body.get("format")
                title = fmt.get("title", "") if isinstance(fmt, dict) else ""
                result = answer(title, body.get("messages", []))
                content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                base = {"model": body.get("model", "qwen-fake"), "created_at": "2026-10-04T00:00:00Z"}
                chunks = [{**base, "message": {"role": "assistant", "content": content}, "done": False},
                          {**base, "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                           "total_duration": 1, "load_duration": 1, "prompt_eval_count": 1, "prompt_eval_duration": 1,
                           "eval_count": 1, "eval_duration": 1}]
                if body.get("stream", True):
                    data = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks).encode()
                    self._send(data, "application/x-ndjson")
                else:
                    self._json({**chunks[0], **{k: v for k, v in chunks[1].items() if k != "message"}})

            def _json(self, payload):
                self._send(json.dumps(payload, ensure_ascii=False).encode(), "application/json")

            def _send(self, data: bytes, content_type: str):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOllama":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Имитация Ollama (не Qwen) для показа интерфейса без модели")
    parser.add_argument("--port", type=int, default=11500)
    parser.add_argument("--delay", type=float, default=0.0, help="задержка ответа, секунд")
    args = parser.parse_args()
    with FakeOllama(args.port, args.delay) as fake:
        print(f"Имитация Ollama на {fake.url} (ответы по правилам, не Qwen). Ctrl+C — остановить.")
        try:
            fake.thread.join()
        except KeyboardInterrupt:
            pass
