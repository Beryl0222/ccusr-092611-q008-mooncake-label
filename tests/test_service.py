"""服务层：导入去重、发布阻断、钉版订单、撤回、重启一致性。"""
import os
import tempfile
import unittest

from mooncake_label.service import LabelService, PublishBlocked, ServiceError


def lotus(product_id="P-LOTUS", version="N-2026-1", origin="广东",
          allergens=None, sugar=22.0, sodium=250, energy=1674):
    return {
        "product_id": product_id,
        "nutrition_version": version,
        "ingredient_version": "I-" + version[2:],
        "origin": origin,
        "basis": "per_100g",
        "nutrients": {
            "energy": {"value": energy, "unit": "kJ"},
            "fat": 18.0,
            "sodium": sodium,
            "sugar": sugar,
        },
        "allergens": allergens or [],
    }


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.svc = LabelService()

    def tearDown(self):
        self.svc.close()

    # -- 导入与去重 ---------------------------------------------------------

    def test_import_dedup_batch_and_alerts(self):
        a = self.svc.import_labels([lotus()], batch_id="B1")
        self.assertEqual(a["alerts_new"], 0)
        self.assertEqual(a["labels_inserted"], 1)
        # 同一批次重复导入：回放，告警不重复
        b = self.svc.import_labels([lotus()], batch_id="B1")
        self.assertEqual(b, a)
        self.assertEqual(len(self.svc.list_alerts()), 0)

    def test_reimport_same_product_does_not_duplicate_alert(self):
        bad = lotus(product_id="P-BAD", sodium={"value": 1, "unit": "杯"})
        first = self.svc.import_labels([bad], batch_id="B2")
        self.assertEqual(first["alerts_new"], 1)
        self.assertEqual(first["invalid_count"], 1)
        # 另一批次重复导入同一批商品：版本已存在，唯一约束兜底不重复告警
        second = self.svc.import_labels([bad], batch_id="B3")
        self.assertEqual(second["labels_inserted"], 0)
        self.assertEqual(second["alerts_new"], 0)
        self.assertEqual(len(self.svc.list_alerts()), 1)

    def test_invalid_label_not_recommendable(self):
        self.svc.import_labels(
            [lotus(product_id="P-OK"), lotus(product_id="P-BAD", sugar=999)],
            batch_id="B4")
        with self.assertRaises(ServiceError):
            self.svc.trial(product_ids=["P-BAD"])
        # 有效商品正常试算
        result = self.svc.trial(product_ids=["P-OK"])
        self.assertEqual(result["products"], ["P-OK"])

    def test_new_label_version_is_traceable(self):
        self.svc.import_labels([lotus(version="N-2026-1", sugar=22)], batch_id="B5")
        self.svc.import_labels([lotus(version="N-2026-2", sugar=8)], batch_id="B6")
        product = self.svc.get_product("P-LOTUS")
        versions = [v["label_version"] for v in product["versions"]]
        self.assertEqual(versions, ["N-2026-1", "N-2026-2"])
        latest = self.svc.trial(product_ids=["P-LOTUS"])["results"][0]
        self.assertEqual(latest["label_version"], "N-2026-2")

    # -- 发布阻断与撤回 -----------------------------------------------------

    def test_allergen_conflict_blocks_publish(self):
        self.svc.create_profile({"profile_id": "u1", "scopes": ["allergy"],
                                 "allergens": ["peanut"]})
        self.svc.import_labels([lotus(allergens=["peanut"])], batch_id="B7")
        with self.assertRaises(PublishBlocked) as ctx:
            self.svc.publish("REL-1", profile_ids=["u1"])
        self.assertEqual(len(ctx.exception.conflicts), 1)
        # 没有发布单落库
        with self.assertRaises(ServiceError):
            self.svc.get_release("REL-1")
        # 阻断事件留了审计
        blocked = [e for e in self.svc.audit_tail("release", "REL-1")
                   if e["action"] == "release.blocked"]
        self.assertEqual(len(blocked), 1)

    def test_publish_then_withdraw_blocks_orders(self):
        self.svc.import_labels([lotus()], batch_id="B8")
        pub = self.svc.publish("REL-2")
        self.assertEqual(pub["status"], "published")
        self.svc.create_order("O1", "P-LOTUS")
        self.svc.withdraw("REL-2")
        with self.assertRaises(ServiceError):
            self.svc.create_order("O2", "P-LOTUS")

    def test_withdraw_idempotent(self):
        self.svc.import_labels([lotus()], batch_id="B9")
        self.svc.publish("REL-3")
        w1 = self.svc.withdraw("REL-3", request_key="w1")
        w2 = self.svc.withdraw("REL-3", request_key="w1")
        self.assertEqual(w1, w2)
        audit = [e for e in self.svc.audit_tail("release", "REL-3")
                 if e["action"] == "release.withdraw"]
        self.assertEqual(len(audit), 1)

    # -- 授权范围 -----------------------------------------------------------

    def test_profile_out_of_scope_ignored_in_publish_advice(self):
        self.svc.create_profile({"profile_id": "u2", "scopes": [],
                                 "diabetes": True})
        self.svc.import_labels([lotus(sugar=22)], batch_id="B10")
        pub = self.svc.publish("REL-4", profile_ids=["u2"])
        advice = pub["results"][0]["profiles"]["u2"]
        self.assertIn("diabetes", advice["ignored_concerns"])
        self.assertFalse(any(i["concern"] == "sugar_g" for i in advice["items"]))

    # -- 历史订单钉版 -------------------------------------------------------

    def test_order_pinned_after_rules_update(self):
        self.svc.import_labels([lotus(sugar=22)], batch_id="B11")
        self.svc.publish("REL-5", rule_version="2024.09")
        order = self.svc.create_order("O3", "P-LOTUS")
        self.assertEqual(order["rule_version"], "2024.09")

        # 注册并生效更严格的新规则
        from mooncake_label.domain import DEFAULT_RULES_V2
        self.svc.register_ruleset(
            DEFAULT_RULES_V2.version,
            {n: [{"level": t.level, "max_per_100g": t.max_per_100g}
                 for t in ts]
             for n, ts in DEFAULT_RULES_V2.thresholds.items()})
        self.assertEqual(self.svc.get_ruleset().version, "2026.09")

        # 历史订单仍按 2024.09 解释，快照与重算一致
        got = self.svc.get_order("O3")
        self.assertEqual(got["pinned"]["rule_version"], "2024.09")
        self.assertTrue(got["consistent"])
        self.assertTrue(got["recomputed_under_pinned_rules"]["eligible"])

    def test_order_with_profile_pinned_when_profile_changes(self):
        self.svc.create_profile({"profile_id": "u3", "scopes": ["allergy"],
                                 "allergens": ["milk"]})
        self.svc.import_labels([lotus(allergens=["peanut"])], batch_id="B12")
        self.svc.publish("REL-6", profile_ids=["u3"])
        self.svc.create_order("O4", "P-LOTUS", profile_id="u3")
        # 画像后来新增花生过敏；历史订单解释不变
        self.svc.create_profile({"profile_id": "u3", "scopes": ["allergy"],
                                 "allergens": ["milk", "peanut"]})
        got = self.svc.get_order("O4")
        self.assertTrue(got["consistent"])
        self.assertEqual(got["recomputed_under_pinned_rules"]["blocks"], [])

    # -- 试算 ----------------------------------------------------------------

    def test_trial_with_draft_thresholds(self):
        self.svc.import_labels([lotus(sugar=12)], batch_id="B13")
        trial = self.svc.trial(thresholds={
            "sugar_g": [{"level": "caution", "max_per_100g": 5},
                        {"level": "avoid", "max_per_100g": 10}]})
        item = trial["results"][0]["general"]["items"][0]
        self.assertEqual(item["severity"], "avoid")
        # 试算不改变生效规则
        self.assertEqual(self.svc.get_ruleset().version, "2024.09")


class PersistenceTests(unittest.TestCase):
    def test_restart_keeps_results_and_audit(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            svc = LabelService(path)
            svc.import_labels([lotus()], batch_id="B20")
            svc.publish("REL-20")
            svc.create_order("O20", "P-LOTUS")
            alerts_before = len(svc.list_alerts())
            audit_before = len(svc.audit_tail())
            svc.close()

            svc2 = LabelService(path)
            self.assertEqual(svc2.get_release("REL-20")["status"], "published")
            order = svc2.get_order("O20")
            self.assertTrue(order["consistent"])
            self.assertEqual(len(svc2.list_alerts()), alerts_before)
            self.assertEqual(len(svc2.audit_tail()), audit_before)
            # 重启后重复导入同批次仍不重复告警
            again = svc2.import_labels([lotus()], batch_id="B20")
            self.assertEqual(again["labels_inserted"], 1)
            self.assertEqual(len(svc2.list_alerts()), alerts_before)
            svc2.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
