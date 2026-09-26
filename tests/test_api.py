"""HTTP 边界冒烟测试。"""
import json
import threading
import unittest
from http.client import HTTPConnection

from mooncake_label.api import make_handler
from mooncake_label.service import DomainStore, LabelService
from http.server import ThreadingHTTPServer


def lotus(product_id="P-WEB", allergens=None):
    return {
        "product_id": product_id,
        "nutrition_version": "N-1",
        "ingredient_version": "I-1",
        "origin": "广东",
        "basis": "per_100g",
        "nutrients": {"energy": {"value": 1674, "unit": "kJ"},
                      "fat": 18, "sodium": 250, "sugar": 22},
        "allergens": allergens or [],
    }


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.svc = LabelService()
        handler = make_handler(self.svc, DomainStore())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.svc.close()

    def call(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_import_trial_publish_withdraw_flow(self):
        status, imported = self.call("POST", "/imports",
                                     {"labels": [lotus()], "batch_id": "WB1"})
        self.assertEqual(status, 200)
        self.assertEqual(imported["invalid_count"], 0)

        status, trial = self.call("POST", "/trials", {})
        self.assertEqual(status, 200)
        self.assertEqual(trial["rule_version"], "2024.09")

        status, _ = self.call("POST", "/releases", {"release_id": "WR1"})
        self.assertEqual(status, 200)

        status, data = self.call("POST", "/releases/WR1/withdraw", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "withdrawn")

        status, _ = self.call("POST", "/orders",
                              {"order_id": "WO1", "product_id": "P-WEB"})
        self.assertEqual(status, 400)  # 已撤回，无法下单

    def test_allergen_block_returns_422(self):
        self.svc.create_profile({"profile_id": "u1", "scopes": ["allergy"],
                                 "allergens": ["peanut"]})
        status, imported = self.call("POST", "/imports",
                                     {"labels": [lotus(allergens=["peanut"])]})
        self.assertEqual(imported["invalid_count"], 0)
        status, data = self.call("POST", "/releases",
                                 {"release_id": "WR2", "profile_ids": ["u1"]})
        self.assertEqual(status, 422)
        self.assertEqual(len(data["conflicts"]), 1)

    def test_invalid_json_400(self):
        status, _ = self.call_post_raw("POST", "/imports", b"{bad")
        self.assertEqual(status, 400)

    def call_post_raw(self, method, path, raw):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, raw, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_audit_and_product_trace(self):
        self.call("POST", "/imports", {"labels": [lotus()], "batch_id": "WB3"})
        status, data = self.call("GET", "/audit?entity_type=batch")
        self.assertEqual(status, 200)
        self.assertTrue(data["events"])
        status, data = self.call("GET", "/products/P-WEB")
        self.assertEqual(status, 200)
        self.assertEqual(data["versions"][0]["normalized"]["per_100g"]["sugar_g"], 22)


if __name__ == "__main__":
    unittest.main()
