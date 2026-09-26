import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from mooncake_label.api import make_handler
from mooncake_label.labels import LabelService
from mooncake_label.nutrition import DEFAULT_RULE_VERSION, normalize_label
from mooncake_label.service import ServiceError


def make_item(sku="MC-001", key=None, **overrides):
    item = {
        "client_key": key or sku,
        "sku": sku,
        "origin": "苏州",
        "label_version": "2025版",
        "declared_allergens": ["gluten"],
        "ingredients": [
            {"name": "小麦粉", "allergens": ["gluten"]},
            {"name": "莲蓉", "allergens": []},
        ],
        "nutrients": [
            {"name": "energy", "value": 1800, "unit": "kJ"},
            {"name": "fat", "value": 20.0, "unit": "g"},
            {"name": "sodium", "value": 300, "unit": "mg"},
            {"name": "sugar", "value": 12.0, "unit": "g"},
        ],
    }
    item.update(overrides)
    return item


class NormalizeTests(unittest.TestCase):
    def test_per_serving_and_kcal_converted_to_per_100g(self):
        nutrients = [
            {"name": "energy", "value": 450, "unit": "kcal", "basis": "per_serving", "serving_size_g": 50},
            {"name": "fat", "value": 10, "unit": "g", "basis": "per_serving", "serving_size_g": 50},
            {"name": "sodium", "value": 200, "unit": "mg", "basis": "per_serving", "serving_size_g": 50},
            {"name": "sugar", "value": 6, "unit": "g", "basis": "per_serving", "serving_size_g": 50},
        ]
        normalized, issues = normalize_label(nutrients)
        self.assertEqual(issues, [])
        self.assertAlmostEqual(normalized["energy"], 450 * 4.184 * 2, places=3)
        self.assertEqual(normalized["fat"], 20.0)
        self.assertEqual(normalized["sodium"], 400.0)
        self.assertEqual(normalized["sugar"], 12.0)

    def test_unit_anomalies_flagged(self):
        nutrients = [
            {"name": "energy", "value": 5000, "unit": "kJ"},                    # 超出合理性上限
            {"name": "fat", "value": 20, "unit": "oz"},                         # 未知单位
            {"name": "sodium", "value": -1, "unit": "mg"},                      # 负值
            {"name": "sugar", "value": 5, "unit": "g", "basis": "per_100ml"},   # 异常基准
        ]
        normalized, issues = normalize_label(nutrients)
        self.assertEqual(normalized, {})
        self.assertEqual(
            sorted(issues),
            sorted(["implausible:energy", "bad_unit:fat:oz", "bad_value:sodium", "bad_basis:sugar:per_100ml"]),
        )

    def test_missing_and_duplicate_nutrients_flagged(self):
        _, issues = normalize_label([{"name": "energy", "value": 1000, "unit": "kJ"}])
        self.assertIn("missing:fat", issues)
        self.assertIn("missing:sodium", issues)
        self.assertIn("missing:sugar", issues)
        _, issues = normalize_label([
            {"name": "energy", "value": 1000, "unit": "kJ"},
            {"name": "fat", "value": 1, "unit": "g"},
            {"name": "fat", "value": 2, "unit": "g"},
            {"name": "sodium", "value": 100, "unit": "mg"},
            {"name": "sugar", "value": 5, "unit": "g"},
        ])
        self.assertIn("duplicate_nutrient:fat", issues)


class LabelServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = LabelService()

    def tearDown(self):
        self.service.close()

    def _import(self, batch="B-001", items=None):
        return self.service.import_batch(batch, items if items is not None else [make_item()])

    def _published_label(self, sugar=12.0, sku="MC-001", batch="B-001", declared=None):
        item = make_item(sku=sku, key=sku)
        for entry in item["nutrients"]:
            if entry["name"] == "sugar":
                entry["value"] = sugar
        if declared is not None:
            item["declared_allergens"] = declared
        self.service.import_batch(batch, [item])
        label_id = f"{batch}:{sku}"
        self.service.publish_label(label_id, f"pub-{batch}-{sku}")
        return label_id

    def test_reimport_same_batch_is_idempotent(self):
        bad = make_item(sku="MC-002", key="MC-002",
                        nutrients=[{"name": "energy", "value": 1000, "unit": "kJ"}])
        items = [make_item(), bad]
        first = self._import(items=items)
        self.assertEqual(first["imported"], 2)
        self.assertEqual(len(self.service.list_alerts()), 1)  # 缺三项营养 → 一条 missing_nutrient
        again = self._import(items=items)
        self.assertEqual(first, again)                          # 重复导入返回首次结果
        self.assertEqual(len(self.service.list_alerts()), 1)    # 不重复生成告警
        self.assertEqual(len(self.service.list_labels()), 2)
        with self.assertRaises(ServiceError) as ctx:
            self.service.import_batch("B-001", [make_item(sku="MC-009", key="MC-009")])
        self.assertEqual(ctx.exception.code, "conflict")        # 同批次号不同内容 → 冲突

    def test_invalid_label_cannot_publish_nor_recommend(self):
        bad = make_item(nutrients=[{"name": "energy", "value": 1000, "unit": "kJ"}])
        unpublished = make_item(sku="MC-003", key="MC-003")
        self._import(items=[bad, unpublished])
        label_id = "B-001:MC-001"
        self.assertEqual(self.service.get_label(label_id)["status"], "invalid")
        with self.assertRaises(ServiceError):
            self.service.publish_label(label_id, "pub-1")
        self.service.upsert_profile("P1", {"diabetic": True}, ["diabetes"])
        record = self.service.recommend("P1", "rec-1")
        self.assertEqual(record["results"], [])  # invalid 与未发布标签都不得进入推荐

    def test_allergen_conflict_blocks_publish(self):
        item = make_item(declared_allergens=[],
                         ingredients=[{"name": "花生酱", "allergens": ["peanut"]}])
        self._import(items=[item])
        label_id = "B-001:MC-001"
        with self.assertRaises(ServiceError) as ctx:
            self.service.publish_label(label_id, "pub-1")
        self.assertEqual(ctx.exception.code, "conflict")
        label = self.service.get_label(label_id)
        self.assertEqual(label["status"], "valid")  # 阻止发布：状态不变
        self.assertIn("allergen_conflict", [a["kind"] for a in self.service.list_alerts()])
        self.assertIn("publish_blocked", [e["kind"] for e in self.service.label_events(label_id)])

    def test_free_from_claim_conflict_blocks_publish(self):
        self._import(items=[make_item(free_from=["gluten"])])
        with self.assertRaises(ServiceError):
            self.service.publish_label("B-001:MC-001", "pub-1")

    def test_publish_withdraw_and_idempotent_replay(self):
        self._import()
        label_id = "B-001:MC-001"
        first = self.service.publish_label(label_id, "pub-1")
        replay = self.service.publish_label(label_id, "pub-1")  # 同一请求键重放
        self.assertEqual(first, replay)
        self.assertEqual(self.service.get_label(label_id)["version"], 2)
        with self.assertRaises(ServiceError):
            self.service.publish_label(label_id, "pub-2")       # 已发布不能重复发布
        self.service.withdraw_label(label_id, "wd-1")
        self.assertEqual(self.service.get_label(label_id)["status"], "withdrawn")
        self.service.upsert_profile("P1", {"diabetic": True}, ["diabetes"])
        record = self.service.recommend("P1", "rec-1")
        self.assertEqual(record["results"], [])  # 已撤回不得进入推荐

    def test_rule_versions_and_historical_evaluation(self):
        self._published_label(12.0)
        self.service.upsert_profile("P1", {"diabetic": True}, ["diabetes"])
        first = self.service.recommend("P1", "rec-1")
        self.assertEqual(first["rule_version"], DEFAULT_RULE_VERSION)
        self.assertEqual(first["results"][0]["verdicts"]["diabetes"], "caution")
        self.service.create_rules("v2", {"diabetic_sugar_warn_g": 10.0})  # 更严的糖阈值
        second = self.service.recommend("P1", "rec-2")
        self.assertEqual(second["rule_version"], "v2")
        self.assertEqual(second["results"][0]["verdicts"]["diabetes"], "avoid")
        history = self.service.get_evaluation(first["eval_id"])  # 历史订单仍按当时版本解释
        self.assertEqual(history["rule_version"], DEFAULT_RULE_VERSION)
        self.assertEqual(history["results"][0]["verdicts"]["diabetes"], "caution")
        self.assertEqual(history["rules"]["diabetic_sugar_warn_g"], 15.0)

    def test_dry_run_does_not_persist(self):
        self._published_label(12.0)
        events_before = len(self.service.list_events())
        trial = self.service.dry_run_rules({"diabetic_sugar_warn_g": 8.0})
        self.assertEqual(trial["rule_version"], "candidate")
        self.assertEqual(trial["results"][0]["verdicts"]["diabetes"], "avoid")
        self.assertEqual(len(self.service.list_events()), events_before)  # 试算不落库

    def test_profile_scopes_limit_computation(self):
        self._published_label(12.0, declared=["gluten", "peanut"])
        self.service.upsert_profile(
            "P1",
            {"diabetic": True, "gi_sensitive": True, "allergens": ["peanut"]},
            ["allergy"],
        )
        record = self.service.recommend("P1", "rec-1")
        verdicts = record["results"][0]["verdicts"]
        self.assertEqual(set(verdicts), {"allergy"})  # 未授权范围不参与计算
        self.assertEqual(verdicts["allergy"], "avoid")
        self.assertEqual(sorted(record["unauthorized_skipped"]), ["diabetes", "gi"])

    def test_reminders_never_contradict(self):
        low = self._published_label(4.0, sku="MC-LOW", batch="B-LOW")
        high = self._published_label(20.0, sku="MC-HIGH", batch="B-HIGH")
        self.service.upsert_profile("P1", {"diabetic": True}, ["diabetes"])
        record = self.service.recommend("P1", "rec-1")
        by_id = {r["label_id"]: r for r in record["results"]}
        self.assertEqual(by_id[low]["overall"], "suitable")
        self.assertEqual(by_id[high]["overall"], "avoid")
        for result in record["results"]:
            # 每个关注点只有一个结论，提醒由结论派生
            self.assertEqual(len(result["verdicts"]), len(set(result["verdicts"])))
            text = "".join(result["reminders"])
            self.assertFalse("可适量食用" in text and "不建议" in text)

    def test_allergy_verdict_forces_overall_avoid(self):
        self._published_label(4.0, declared=["gluten", "peanut"])
        self.service.upsert_profile(
            "P1", {"diabetic": True, "allergens": ["peanut"]}, ["diabetes", "allergy"]
        )
        record = self.service.recommend("P1", "rec-1")
        result = record["results"][0]
        self.assertEqual(result["verdicts"]["diabetes"], "suitable")
        self.assertEqual(result["verdicts"]["allergy"], "avoid")
        self.assertEqual(result["overall"], "avoid")

    def test_restart_keeps_results_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/labels.db"
            service = LabelService(path)
            service.import_batch("B-001", [make_item()])
            service.import_batch("B-002", [make_item(
                sku="MC-002", key="MC-002",
                nutrients=[{"name": "energy", "value": 1000, "unit": "kJ"}])])
            service.publish_label("B-001:MC-001", "pub-1")
            service.upsert_profile("P1", {"diabetic": True}, ["diabetes"])
            record = service.recommend("P1", "rec-1")
            service.create_rules("v2", {"diabetic_sugar_warn_g": 10.0})
            alerts = service.list_alerts()
            events = service.list_events()
            self.assertTrue(alerts)
            service.close()

            reopened = LabelService(path)
            try:
                self.assertEqual(reopened.get_label("B-001:MC-001")["status"], "published")
                self.assertEqual(reopened.get_evaluation(record["eval_id"]), record)
                self.assertEqual(reopened.list_alerts(), alerts)
                self.assertEqual(reopened.list_events(), events)
                replay = reopened.recommend("P1", "rec-1")  # 重启后重复请求仍幂等
                self.assertEqual(replay["eval_id"], record["eval_id"])
                self.assertEqual(reopened.list_rules()["active"], "v2")
            finally:
                reopened.close()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.service = LabelService()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.close()

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        return response.status, payload

    def test_import_publish_recommend_flow(self):
        status, body = self._request("POST", "/batches/import", {"batch_id": "B-1", "items": [make_item()]})
        self.assertEqual(status, 200)
        self.assertEqual(body["imported"], 1)
        status, body = self._request("POST", "/labels/B-1:MC-001/publish", {"request_key": "k1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "published")
        self._request("POST", "/profiles",
                      {"profile_id": "P1", "tags": {"diabetic": True}, "scopes": ["diabetes"]})
        status, body = self._request("POST", "/evaluations", {"profile_id": "P1", "request_key": "e1"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 1)
        status, body = self._request("GET", f"/evaluations/{body['eval_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(body["rule_version"], DEFAULT_RULE_VERSION)
        status, body = self._request("GET", "/audit")
        self.assertEqual(status, 200)
        self.assertTrue(body["events"])

    def test_conflict_and_not_found_status(self):
        self._request("POST", "/batches/import",
                      {"batch_id": "B-1", "items": [make_item(declared_allergens=[])]})
        status, body = self._request("POST", "/labels/B-1:MC-001/publish", {"request_key": "k1"})
        self.assertEqual(status, 409)  # 过敏原冲突阻止发布
        self.assertEqual(body["code"], "conflict")
        status, _ = self._request("GET", "/labels/nope")
        self.assertEqual(status, 404)
        status, _ = self._request("GET", "/unknown")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
