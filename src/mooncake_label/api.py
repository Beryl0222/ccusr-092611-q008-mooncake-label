"""月饼营养核验库的轻量 HTTP 边界。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .labels import LabelService
from .service import ServiceError

_STATUS_BY_CODE = {"bad_request": 400, "not_found": 404, "conflict": 409}


def make_handler(service):
    """把 LabelService 暴露为无页面依赖的本地 JSON 接口。"""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _payload(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ServiceError("请求体不是合法 JSON") from exc

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                self._reply(200, self._dispatch(method, parts))
            except ServiceError as exc:
                self._reply(_STATUS_BY_CODE.get(exc.code, 400), {"error": str(exc), "code": exc.code})

        def _dispatch(self, method, parts):
            if method == "POST" and parts == ["batches", "import"]:
                data = self._payload()
                return service.import_batch(data.get("batch_id"), data.get("items"), data.get("request_key"))
            if method == "POST" and parts == ["rules"]:
                data = self._payload()
                return service.create_rules(data.get("version"), data.get("rules"), data.get("activate", True))
            if method == "GET" and parts == ["rules"]:
                return service.list_rules()
            if method == "POST" and parts == ["rules", "dry-run"]:
                data = self._payload()
                return service.dry_run_rules(
                    data.get("rules"), data.get("rule_version"), data.get("label_ids"),
                    data.get("tags"), data.get("scopes"),
                )
            if method == "GET" and parts == ["labels"]:
                return {"labels": service.list_labels()}
            if method == "GET" and len(parts) == 2 and parts[0] == "labels":
                return service.get_label(parts[1])
            if method == "GET" and len(parts) == 3 and parts[0] == "labels" and parts[2] == "events":
                return {"events": service.label_events(parts[1])}
            if method == "POST" and len(parts) == 3 and parts[0] == "labels" and parts[2] in ("publish", "withdraw"):
                key = self._payload().get("request_key")
                if parts[2] == "publish":
                    return service.publish_label(parts[1], key)
                return service.withdraw_label(parts[1], key)
            if method == "POST" and parts == ["profiles"]:
                data = self._payload()
                return service.upsert_profile(data.get("profile_id"), data.get("tags"), data.get("scopes"))
            if method == "GET" and len(parts) == 2 and parts[0] == "profiles":
                return service.get_profile(parts[1])
            if method == "POST" and parts == ["evaluations"]:
                data = self._payload()
                return service.recommend(
                    data.get("profile_id"), data.get("request_key"),
                    data.get("label_ids"), data.get("rule_version"),
                )
            if method == "GET" and len(parts) == 2 and parts[0] == "evaluations":
                return service.get_evaluation(parts[1])
            if method == "GET" and parts == ["alerts"]:
                return {"alerts": service.list_alerts()}
            if method == "GET" and parts == ["audit"]:
                return {"events": service.list_events()}
            raise ServiceError("接口不存在", "not_found")

        def log_message(self, *_):
            return

    return Handler


def serve(host="127.0.0.1", port=8080, database="mooncake_label.db"):
    ThreadingHTTPServer((host, port), make_handler(LabelService(database))).serve_forever()
