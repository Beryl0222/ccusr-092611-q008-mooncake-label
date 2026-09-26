"""标签核验服务的轻量 HTTP 边界（无外部依赖）。

路由（JSON 进出）：
  POST /imports            批量导入标签 {"labels":[...], "batch_id"?, "request_key"?}
  POST /trials             规则试算 {"product_ids"?, "profile_ids"?, "rule_version"? | "thresholds"?}
  POST /releases           发布 {"release_id", "product_ids"?, "profile_ids"?, "rule_version"?, "request_key"?}
  POST /releases/{id}/withdraw  撤回
  POST /orders             下单 {"order_id","product_id","profile_id"?}
  POST /profiles           登记画像（含授权范围 scopes）
  POST /rules              注册新规则版本（自动生效，旧版本保留供钉版解释）
  GET  /products/{id}      商品全部标签版本（可追溯）
  GET  /releases/{id}      发布单
  GET  /orders/{id}        订单按钉住规则版本重算并比对快照
  GET  /alerts[?batch_id=] 告警列表
  GET  /audit[?entity_type=&entity_id=] 审计事件
  GET  /records/{id}       旧版通用记录（兼容）
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .service import DomainStore, LabelService, PublishBlocked, ServiceError


def make_handler(service: LabelService, store: DomainStore | None = None):
    store = store or DomainStore()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise ServiceError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ServiceError("请求体必须是 JSON 对象")
            return data

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if method == "POST":
                    result = self._post(path)
                else:
                    result = self._get(path, query)
                self._reply(200, result)
            except PublishBlocked as exc:
                self._reply(422, {"error": str(exc), "conflicts": exc.conflicts})
            except ServiceError as exc:
                self._reply(400, {"error": str(exc)})
            except ValueError as exc:
                self._reply(400, {"error": str(exc)})
            except KeyError as exc:
                self._reply(400, {"error": f"缺少字段: {exc}"})

        def _post(self, path: str):
            body = self._body()
            if path == "/imports":
                return service.import_labels(
                    body["labels"], batch_id=body.get("batch_id"),
                    request_key=body.get("request_key"))
            if path == "/trials":
                return service.trial(
                    product_ids=body.get("product_ids"),
                    profile_ids=body.get("profile_ids"),
                    rule_version=body.get("rule_version"),
                    thresholds=body.get("thresholds"))
            if path == "/releases":
                return service.publish(
                    body["release_id"], product_ids=body.get("product_ids"),
                    profile_ids=body.get("profile_ids"),
                    rule_version=body.get("rule_version"),
                    note=body.get("note", ""),
                    request_key=body.get("request_key"))
            if path.startswith("/releases/") and path.endswith("/withdraw"):
                release_id = path.split("/")[2]
                return service.withdraw(release_id, request_key=body.get("request_key"))
            if path == "/orders":
                return service.create_order(
                    body["order_id"], body["product_id"],
                    profile_id=body.get("profile_id"),
                    request_key=body.get("request_key"))
            if path == "/profiles":
                return service.create_profile(body, request_key=body.get("request_key"))
            if path == "/rules":
                return service.register_ruleset(
                    body["version"], body["thresholds"],
                    note=body.get("note", ""), request_key=body.get("request_key"))
            raise ServiceError(f"未知接口: POST {path}")

        def _get(self, path: str, query: dict):
            if path.startswith("/products/"):
                return service.get_product(path.rsplit("/", 1)[-1])
            if path.startswith("/releases/"):
                return service.get_release(path.rsplit("/", 1)[-1])
            if path.startswith("/orders/"):
                return service.get_order(path.rsplit("/", 1)[-1])
            if path == "/alerts":
                return {"alerts": service.list_alerts(query.get("batch_id", [None])[0])}
            if path == "/audit":
                return {"events": service.audit_tail(
                    query.get("entity_type", [None])[0],
                    query.get("entity_id", [None])[0])}
            if path.startswith("/records/"):
                return store.get(path.rsplit("/", 1)[-1]).__dict__
            raise ServiceError(f"未知接口: GET {path}")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_):
            return

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8080,
          database: str = "labels.db") -> None:
    """默认使用文件数据库，重启后结果与审计记录保持一致。"""
    service = LabelService(database)
    ThreadingHTTPServer((host, port), make_handler(service)).serve_forever()


if __name__ == "__main__":
    serve()
