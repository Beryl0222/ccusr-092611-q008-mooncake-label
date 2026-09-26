"""营养成分的每百克统一换算与规则评估（纯函数，不落库）。"""

# 统一目标单位：能量 kJ/100g、脂肪 g/100g、钠 mg/100g、糖 g/100g。
NUTRIENTS = ("energy", "fat", "sodium", "sugar")

_ENERGY_TO_KJ = {"kJ": 1.0, "kcal": 4.184}
_MASS_TO_G = {"g": 1.0, "mg": 1e-3, "kg": 1e3, "ug": 1e-6, "μg": 1e-6}
_UNIT_TABLES = {"energy": _ENERGY_TO_KJ, "fat": _MASS_TO_G, "sodium": _MASS_TO_G, "sugar": _MASS_TO_G}
# 钠的目标单位是 mg，其余质量类按 g 计。
_OUTPUT_SCALE = {"sodium": 1e3}
# 每百克合理性上限，超出视为单位异常（例如把每份误标成每百克）。
_PLAUSIBLE_MAX = {"energy": 4000.0, "fat": 100.0, "sodium": 100000.0, "sugar": 100.0}

DEFAULT_RULE_VERSION = "builtin-v1"
# 默认核验规则，阈值单位与换算后的每百克目标单位一致。
DEFAULT_RULES = {
    "diabetic_sugar_low_g": 5.0,
    "diabetic_sugar_warn_g": 15.0,
    "gi_fat_warn_g": 15.0,
    "sodium_warn_mg": 600.0,
    "energy_warn_kj": 1800.0,
}

# 顾客画像的授权范围：糖尿病、过敏、胃肠不适。
CONCERN_SCOPES = ("diabetes", "allergy", "gi")

_SEVERITY = {"suitable": 0, "caution": 1, "avoid": 2}

_REMINDER_TEXT = {
    ("diabetes", "suitable"): "低糖，糖尿病人群可适量食用",
    ("diabetes", "caution"): "含糖中等，糖尿病人群需控制食用量",
    ("diabetes", "avoid"): "高糖，不建议糖尿病人群食用",
    ("allergy", "suitable"): "未检出已申报过敏原，过敏人群可食用",
    ("gi", "suitable"): "脂肪较低，胃肠不适人群可食用",
    ("gi", "caution"): "脂肪偏高，胃肠不适人群慎食",
    ("gi", "avoid"): "脂肪过高，胃肠不适人群不宜食用",
}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_label(entries):
    """把营养表统一换算到每百克目标单位。

    返回 (normalized, issues)；issues 非空（缺失、单位异常、数值异常）时标签不得进入推荐。
    """
    normalized, issues, seen = {}, [], set()
    for entry in entries or []:
        name = entry.get("name")
        if name not in _UNIT_TABLES:
            continue  # 非核心营养素不参与核验
        if name in seen:
            issues.append(f"duplicate_nutrient:{name}")
            continue
        seen.add(name)
        value, unit, basis = entry.get("value"), entry.get("unit"), entry.get("basis", "per_100g")
        if not _is_number(value) or value < 0:
            issues.append(f"bad_value:{name}")
            continue
        table = _UNIT_TABLES[name]
        if unit not in table:
            issues.append(f"bad_unit:{name}:{unit}")
            continue
        factor = table[unit]
        if basis == "per_serving":
            serving = entry.get("serving_size_g")
            if not _is_number(serving) or serving <= 0:
                issues.append(f"bad_basis:{name}:per_serving")
                continue
            factor *= 100.0 / serving
        elif basis != "per_100g":
            issues.append(f"bad_basis:{name}:{basis}")
            continue
        converted = value * factor * _OUTPUT_SCALE.get(name, 1.0)
        if converted > _PLAUSIBLE_MAX[name] + 1e-9:
            issues.append(f"implausible:{name}")
            continue
        normalized[name] = round(converted, 3)
    for name in NUTRIENTS:
        if name not in seen:
            issues.append(f"missing:{name}")
    return normalized, issues


def allergen_conflicts(payload):
    """比对原料隐含过敏原与标签申报；返回 (未申报的过敏原, 与“不含”宣称冲突的过敏原)。"""
    payload = payload or {}
    implied = set()
    for ingredient in payload.get("ingredients") or []:
        implied |= set(ingredient.get("allergens") or [])
    declared = set(payload.get("declared_allergens") or [])
    free_from = set(payload.get("free_from") or [])
    return sorted(implied - declared), sorted(implied & free_from)


def merge_rules(body):
    """把候选规则合并到默认规则上，并校验阈值非负。"""
    merged = dict(DEFAULT_RULES)
    for key, value in (body or {}).items():
        if key not in merged:
            raise ValueError(f"未知规则项:{key}")
        if not _is_number(value) or value < 0:
            raise ValueError(f"规则阈值非法:{key}")
        merged[key] = float(value)
    return merged


def evaluate_label(normalized, declared_allergens, tags, scopes, rules):
    """按授权范围评估单个标签。

    每个关注点至多产生一个结论，提醒文案由结论派生，因此不会互相矛盾；
    未授权的范围不参与计算。
    """
    tags, scopes = tags or {}, set(scopes or [])
    verdicts, allergy_hits = {}, []

    if "diabetes" in scopes and tags.get("diabetic"):
        sugar = normalized["sugar"]
        if sugar <= rules["diabetic_sugar_low_g"]:
            verdicts["diabetes"] = "suitable"
        elif sugar <= rules["diabetic_sugar_warn_g"]:
            verdicts["diabetes"] = "caution"
        else:
            verdicts["diabetes"] = "avoid"

    if "allergy" in scopes:
        allergy_hits = sorted(set(tags.get("allergens") or []) & set(declared_allergens or []))
        verdicts["allergy"] = "avoid" if allergy_hits else "suitable"

    if "gi" in scopes and tags.get("gi_sensitive"):
        fat, warn = normalized["fat"], rules["gi_fat_warn_g"]
        if fat <= warn:
            verdicts["gi"] = "suitable"
        elif fat <= 2 * warn:
            verdicts["gi"] = "caution"
        else:
            verdicts["gi"] = "avoid"

    reminders = []
    for concern, verdict in verdicts.items():
        if concern == "allergy" and verdict == "avoid":
            reminders.append(f"含过敏原 {','.join(allergy_hits)}，过敏人群禁止食用")
        else:
            reminders.append(_REMINDER_TEXT[(concern, verdict)])

    notices = []
    if normalized["sodium"] > rules["sodium_warn_mg"]:
        notices.append(f"钠含量偏高（{normalized['sodium']}mg/100g）")
    if normalized["energy"] > rules["energy_warn_kj"]:
        notices.append(f"能量偏高（{normalized['energy']}kJ/100g）")

    overall = "not_evaluated"
    if verdicts:
        overall = max(verdicts.values(), key=lambda v: _SEVERITY[v])
    return {"verdicts": verdicts, "overall": overall, "reminders": reminders, "notices": notices}
