"""领域换算与建议引擎测试。"""
import unittest

from mooncake_label.domain import (
    DEFAULT_RULES_V1,
    DEFAULT_RULES_V2,
    build_profile,
    evaluate,
    normalize_label,
)


def lotus_label(**over):
    raw = {
        "product_id": "P-LOTUS",
        "nutrition_version": "N-2026-1",
        "ingredient_version": "I-2026-1",
        "origin": "广东",
        "basis": "per_100g",
        "nutrients": {
            # 1674 kJ ≈ 400 kcal
            "energy": {"value": 1674, "unit": "kJ"},
            "fat": 18.0,
            "sodium": 250,
            "sugar": 22.0,
        },
    }
    raw.update(over)
    return raw


class NormalizeTests(unittest.TestCase):
    def test_kj_and_per100g(self):
        n = normalize_label(lotus_label())
        self.assertTrue(n.valid, [i.message for i in n.issues])
        self.assertAlmostEqual(n.per_100g["energy_kcal"], 400.096, places=1)
        self.assertEqual(n.per_100g["fat_g"], 18.0)
        self.assertEqual(n.per_100g["sodium_mg"], 250.0)
        self.assertEqual(n.per_100g["sugar_g"], 22.0)

    def test_per_serving_scales_to_100g(self):
        raw = lotus_label(
            basis="per_serving", serving_size_g=50,
            nutrients={"energy": 200, "fat": 9, "sodium": 125, "sugar": 11})
        n = normalize_label(raw)
        self.assertTrue(n.valid, [i.message for i in n.issues])
        self.assertEqual(n.per_100g["energy_kcal"], 400.0)
        self.assertEqual(n.per_100g["sugar_g"], 22.0)
        self.assertEqual(n.per_100g["sodium_mg"], 250.0)

    def test_unit_conversions(self):
        n = normalize_label(lotus_label(nutrients={
            "energy": {"value": 400, "unit": "kcal"},
            "fat": {"value": 18000, "unit": "mg"},
            "sodium": {"value": 0.25, "unit": "g"},
            "sugar": {"value": 22000, "unit": "mg"},
        }))
        self.assertTrue(n.valid, [i.message for i in n.issues])
        self.assertEqual(n.per_100g["fat_g"], 18.0)
        self.assertEqual(n.per_100g["sodium_mg"], 250.0)
        self.assertEqual(n.per_100g["sugar_g"], 22.0)

    def test_missing_fields_invalid(self):
        n = normalize_label({"product_id": "P1", "nutrients": {"fat": 1}})
        self.assertFalse(n.valid)
        codes = {i.code for i in n.issues}
        self.assertIn("nutrition_version_missing", codes)
        self.assertIn("ingredient_version_missing", codes)
        self.assertIn("energy_kcal_missing", codes)
        self.assertIn("sodium_mg_missing", codes)
        self.assertIn("sugar_g_missing", codes)

    def test_abnormal_unit_invalid(self):
        n = normalize_label(lotus_label(nutrients={
            "energy": 1674, "fat": 18, "sodium": {"value": 1, "unit": "杯"},
            "sugar": 22}))
        self.assertFalse(n.valid)
        self.assertTrue(any(c.code == "sodium_mg_unit_abnormal" for c in n.issues))
        self.assertNotIn("sodium_mg", n.per_100g)

    def test_missing_serving_size_invalid(self):
        n = normalize_label(lotus_label(basis="per_serving"))
        self.assertFalse(n.valid)
        self.assertTrue(any(c.code == "serving_size_missing" for c in n.issues))

    def test_value_out_of_range_invalid(self):
        n = normalize_label(lotus_label(nutrients={
            "energy": 1674, "fat": 18, "sodium": 250, "sugar": 999}))
        self.assertFalse(n.valid)
        self.assertTrue(any(c.code == "sugar_g_value_abnormal" for c in n.issues))


class AdviceTests(unittest.TestCase):
    def setUp(self):
        self.label = normalize_label(lotus_label(allergens=["peanut"]))

    def test_diabetes_v1_caution_and_single_message(self):
        p = build_profile({"profile_id": "u1", "scopes": ["diabetes"],
                           "diabetes": True})
        a = evaluate(self.label, DEFAULT_RULES_V1, p)
        sugar = [i for i in a.items if i["concern"] == "sugar_g"]
        self.assertEqual(len(sugar), 1)          # 糖只出一条，不互相矛盾
        self.assertEqual(sugar[0]["severity"], "caution")
        self.assertTrue(a.eligible)             # v1 下糖 22 仅酌减

    def test_same_label_v2_is_avoid_after_rule_update(self):
        p = build_profile({"profile_id": "u1", "scopes": ["diabetes"],
                           "diabetes": True})
        a = evaluate(self.label, DEFAULT_RULES_V2, p)
        self.assertFalse(a.eligible)
        self.assertIn("2026.09", [i["message"] for i in a.items
                                  if i["concern"] == "sugar_g"][0])

    def test_diabetes_and_gi_share_one_sugar_item(self):
        p = build_profile({"profile_id": "u2",
                           "scopes": ["diabetes", "gi"],
                           "diabetes": True, "gi_sensitive": True})
        a = evaluate(self.label, DEFAULT_RULES_V1, p)
        sugar = [i for i in a.items if i["concern"] == "sugar_g"]
        self.assertEqual(len(sugar), 1)
        self.assertIn("糖尿病顾客", sugar[0]["message"])
        self.assertIn("胃肠不适顾客", sugar[0]["message"])

    def test_unauthorized_concern_does_not_participate(self):
        # 声明糖尿病但未授权：画像不得参与该部分计算
        p = build_profile({"profile_id": "u3", "scopes": [], "diabetes": True})
        a = evaluate(self.label, DEFAULT_RULES_V1, p)
        self.assertIn("diabetes", a.ignored_concerns)
        self.assertFalse(any(i["concern"] == "sugar_g" for i in a.items))

    def test_allergen_conflict_blocks(self):
        p = build_profile({"profile_id": "u4", "scopes": ["allergy"],
                           "allergens": ["peanut"]})
        a = evaluate(self.label, DEFAULT_RULES_V1, p)
        self.assertFalse(a.eligible)
        self.assertEqual(len(a.blocks), 1)
        self.assertTrue(a.blocks[0]["code"].startswith("allergen:peanut"))

    def test_non_conflicting_allergen_ok(self):
        p = build_profile({"profile_id": "u5", "scopes": ["allergy"],
                           "allergens": ["milk"]})
        a = evaluate(self.label, DEFAULT_RULES_V1, p)
        self.assertEqual(a.blocks, ())

    def test_advice_records_versions(self):
        a = evaluate(self.label, DEFAULT_RULES_V1)
        self.assertEqual(a.rule_version, "2024.09")
        self.assertEqual(a.label_version, "N-2026-1")
        self.assertEqual(a.ingredient_version, "I-2026-1")


if __name__ == "__main__":
    unittest.main()
