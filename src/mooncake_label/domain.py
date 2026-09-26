"""月饼营养核验领域对象与纯计算逻辑。

本模块不触碰数据库，所有换算、校验、规则判定都可独立测试：

- 标签（原料表 / 营养表）带版本与产地，换算结果记录所用版本；
- 营养值统一换算为“每 100 克”，能量统一为 kcal；
- 缺失字段或单位异常的标签判定为不可用，不得进入推荐；
- 规则集带版本，建议结果记录规则版本，供历史订单钉版解释；
- 建议引擎按授权范围使用画像，过敏原冲突产出阻断结论。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# ---- 通用 ----------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Record:
    """旧版通用记录对象，保留以兼容既有状态服务。"""
    record_id: str
    owner_id: str
    state: str
    version: int
    updated_at: str


# ---- 营养成分与单位 -------------------------------------------------------

# 规范营养素键 -> (中文名, 规范单位)
NUTRIENTS: dict[str, tuple[str, str]] = {
    "energy_kcal": ("能量", "kcal"),
    "fat_g": ("脂肪", "g"),
    "sodium_mg": ("钠", "mg"),
    "sugar_g": ("糖", "g"),
}

# 各成分允许出现的输入单位
ALLOWED_UNITS: dict[str, set[str]] = {
    "energy_kcal": {"kcal", "kJ", "Cal"},
    "fat_g": {"g", "mg"},
    "sodium_mg": {"mg", "g"},
    "sugar_g": {"g", "mg"},
}

# 输入短名 -> 规范键
NUTRIENT_ALIASES = {
    "energy": "energy_kcal",
    "energy_kcal": "energy_kcal",
    "calories": "energy_kcal",
    "fat": "fat_g",
    "fat_g": "fat_g",
    "sodium": "sodium_mg",
    "sodium_mg": "sodium_mg",
    "sugar": "sugar_g",
    "sugar_g": "sugar_g",
    "sugars": "sugar_g",
}

# 每 100 克合理上限（超出视为单位/数值异常，油脂能量约 900kcal）
SANITY_MAX = {
    "energy_kcal": 900.0,
    "fat_g": 100.0,
    "sodium_mg": 100000.0,   # 约 250g 盐，仅用于拦截量级错误
    "sugar_g": 100.0,
}

KNOWN_ALLERGENS = {
    "peanut": "花生",
    "nuts": "坚果",
    "egg": "蛋",
    "milk": "乳",
    "wheat": "小麦(麸质)",
    "soy": "大豆",
    "sesame": "芝麻",
    "seafood": "水产",
}


@dataclass(frozen=True)
class Issue:
    code: str
    message: str


@dataclass(frozen=True)
class NormalizedLabel:
    """标签核验结果。valid 为 False 时 per_100g 不可用于推荐。"""
    product_id: str
    label_version: str          # 营养表版本
    ingredient_version: str     # 原料表版本
    origin: str
    basis: str
    per_100g: dict[str, float]
    valid: bool
    issues: tuple[Issue, ...] = ()
    allergens: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "label_version": self.label_version,
            "ingredient_version": self.ingredient_version,
            "origin": self.origin,
            "basis": self.basis,
            "per_100g": self.per_100g,
            "valid": self.valid,
            "issues": [{"code": i.code, "message": i.message} for i in self.issues],
            "allergens": sorted(self.allergens),
        }


def _to_canonical(key: str) -> str | None:
    return NUTRIENT_ALIASES.get(key)


def normalize_label(raw: dict[str, Any]) -> NormalizedLabel:
    """把一条原始标签换算成每 100 克规范值，并给出全部核验问题。"""
    product_id = str(raw.get("product_id", "")).strip()
    label_version = str(raw.get("nutrition_version") or raw.get("label_version") or "").strip()
    ingredient_version = str(raw.get("ingredient_version") or "").strip()
    origin = str(raw.get("origin") or "未知产地").strip()
    basis = str(raw.get("basis", "per_100g")).strip()
    serving_size = raw.get("serving_size_g")

    issues: list[Issue] = []
    if not product_id:
        issues.append(Issue("product_id_missing", "缺少商品编号"))
    if not label_version:
        issues.append(Issue("nutrition_version_missing", "缺少营养表版本，无法追溯"))
    if not ingredient_version:
        issues.append(Issue("ingredient_version_missing", "缺少原料表版本，无法追溯"))
    if basis not in {"per_100g", "per_serving"}:
        issues.append(Issue("basis_unknown", f"计量基准无法识别: {basis!r}"))
    if basis == "per_serving":
        if not isinstance(serving_size, (int, float)) or isinstance(serving_size, bool):
            issues.append(Issue("serving_size_missing", "按每份标注但缺少份量克数"))
        elif serving_size <= 0 or serving_size > 5000:
            issues.append(Issue("serving_size_abnormal", f"份量克数异常: {serving_size}"))

    scale = 1.0
    if basis == "per_serving" and isinstance(serving_size, (int, float)) and not isinstance(serving_size, bool) and serving_size > 0:
        scale = 100.0 / float(serving_size)

    per_100g: dict[str, float] = {}
    nutrients = raw.get("nutrients") or {}
    if not isinstance(nutrients, dict):
        issues.append(Issue("nutrients_missing", "缺少营养成分表"))
        nutrients = {}

    for canon, (zh, unit) in NUTRIENTS.items():
        entry = None
        for k, v in nutrients.items():
            if _to_canonical(str(k)) == canon:
                entry = v
                break
        if entry is None:
            issues.append(Issue(f"{canon}_missing", f"{zh}含量缺失"))
            continue
        value, src_unit = _unpack_nutrient(canon, entry)
        if value is None:
            issues.append(Issue(f"{canon}_missing", f"{zh}含量缺失或无法解析"))
            continue
        if not isinstance(src_unit, str) or src_unit not in ALLOWED_UNITS[canon]:
            issues.append(Issue(f"{canon}_unit_abnormal",
                                f"{zh}单位异常: {src_unit!r}，允许 {sorted(ALLOWED_UNITS[canon])}"))
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(float(value)) or math.isinf(float(value)):
            issues.append(Issue(f"{canon}_value_abnormal", f"{zh}数值异常: {value!r}"))
            continue
        value = float(value)
        if value < 0:
            issues.append(Issue(f"{canon}_value_abnormal", f"{zh}数值为负: {value}"))
            continue
        value = _convert_nutrient(canon, value, src_unit) * scale
        if value > SANITY_MAX[canon] + 1e-9:
            issues.append(Issue(f"{canon}_value_abnormal",
                                f"{zh}换算后每100克为 {round(value, 2)}{unit}，超出合理范围"))
            continue
        per_100g[canon] = round(value, 3)

    allergens = frozenset()
    flags = raw.get("allergens") or []
    if flags and not isinstance(flags, list):
        issues.append(Issue("allergens_abnormal", "过敏原标记格式异常"))
    else:
        unknown = sorted({f for f in flags if f not in KNOWN_ALLERGENS})
        if unknown:
            issues.append(Issue("allergen_unknown", f"存在未登记过敏原代码: {unknown}"))
        allergens = frozenset(f for f in flags if f in KNOWN_ALLERGENS)

    return NormalizedLabel(
        product_id=product_id,
        label_version=label_version,
        ingredient_version=ingredient_version,
        origin=origin,
        basis=basis,
        per_100g=per_100g,
        valid=not issues,
        issues=tuple(issues),
        allergens=allergens,
    )


_DEFAULT_UNIT = {"energy_kcal": "kcal", "fat_g": "g",
                 "sodium_mg": "mg", "sugar_g": "g"}


def _unpack_nutrient(canon: str, entry: Any) -> tuple[float | None, Any]:
    """营养值支持裸数字（按默认单位）或 {"value": x, "unit": u}。"""
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        return float(entry), _DEFAULT_UNIT[canon]
    if isinstance(entry, dict):
        return entry.get("value"), entry.get("unit")
    if isinstance(entry, str):
        try:
            return float(entry), _DEFAULT_UNIT[canon]
        except ValueError:
            return None, None
    return None, None


def _convert_nutrient(canon: str, value: float, unit: str) -> float:
    if canon == "energy_kcal":
        if unit == "kJ":
            return value / 4.184
        return value  # kcal / Cal
    if canon == "fat_g" or canon == "sugar_g":
        return value / 1000.0 if unit == "mg" else value
    if canon == "sodium_mg":
        return value * 1000.0 if unit == "g" else value
    return value


# ---- 规则集 ---------------------------------------------------------------

LEVEL_ORDER = {"ok": 0, "caution": 1, "avoid": 2}
LEVEL_WORD = {"caution": "酌减", "avoid": "避免选用"}
# 成分 -> 哪些健康关注会引用它
CONCERN_NUTRIENTS = {
    "diabetes": {"sugar_g", "energy_kcal"},
    "gi": {"sugar_g", "fat_g"},
    "general": {"sodium_mg"},
}
CONCERN_NAME = {"diabetes": "糖尿病顾客", "gi": "胃肠不适顾客", "general": "普通顾客"}


@dataclass(frozen=True)
class Threshold:
    nutrient: str
    level: str
    max_per_100g: float


@dataclass(frozen=True)
class RuleSet:
    version: str
    # nutrient -> [Threshold]，按严格程度排列
    thresholds: dict[str, tuple[Threshold, ...]]
    note: str = ""

    def level_for(self, nutrient: str, value: float) -> Threshold | None:
        """返回该数值触发的最严阈值（已超过的阈值中 max 最大者）。"""
        hit: Threshold | None = None
        for t in self.thresholds.get(nutrient, ()):  # 已按 max 升序
            if value > t.max_per_100g and (hit is None or t.max_per_100g > hit.max_per_100g):
                hit = t
        return hit

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "note": self.note,
            "thresholds": {
                n: [{"level": t.level, "max_per_100g": t.max_per_100g}
                    for t in ts]
                for n, ts in self.thresholds.items()
            },
        }


def build_ruleset(version: str, thresholds: dict[str, list[dict[str, Any]]],
                  note: str = "") -> RuleSet:
    """从接口/存储的 JSON 结构构建规则集，非法定义直接抛错。"""
    if not version or not str(version).strip():
        raise ValueError("规则版本不能为空")
    parsed: dict[str, tuple[Threshold, ...]] = {}
    for nutrient, items in (thresholds or {}).items():
        if nutrient not in NUTRIENTS:
            raise ValueError(f"规则引用了未知营养成分: {nutrient}")
        rows = []
        for item in items:
            level = item["level"]
            limit = item["max_per_100g"]
            if level not in ("caution", "avoid"):
                raise ValueError(f"规则级别非法: {level}")
            if not isinstance(limit, (int, float)) or limit < 0:
                raise ValueError(f"{nutrient} 阈值非法: {limit}")
            rows.append(Threshold(nutrient, level, float(limit)))
        parsed[nutrient] = tuple(sorted(rows, key=lambda t: t.max_per_100g))
    return RuleSet(version=str(version), thresholds=parsed, note=note)


# 默认两套规则：旧版宽松、中秋新版更严，便于演示钉版解释
DEFAULT_RULES_V1 = build_ruleset(
    "2024.09",
    {
        "energy_kcal": [{"level": "caution", "max_per_100g": 380},
                        {"level": "avoid", "max_per_100g": 500}],
        "fat_g": [{"level": "caution", "max_per_100g": 21},
                  {"level": "avoid", "max_per_100g": 35}],
        "sodium_mg": [{"level": "caution", "max_per_100g": 400},
                      {"level": "avoid", "max_per_100g": 800}],
        "sugar_g": [{"level": "caution", "max_per_100g": 15},
                    {"level": "avoid", "max_per_100g": 25}],
    },
    note="中秋标签核验初始规则",
)

DEFAULT_RULES_V2 = build_ruleset(
    "2026.09",
    {
        "energy_kcal": [{"level": "caution", "max_per_100g": 320},
                        {"level": "avoid", "max_per_100g": 450}],
        "fat_g": [{"level": "caution", "max_per_100g": 17},
                  {"level": "avoid", "max_per_100g": 30}],
        "sodium_mg": [{"level": "caution", "max_per_100g": 300},
                      {"level": "avoid", "max_per_100g": 600}],
        "sugar_g": [{"level": "caution", "max_per_100g": 10},
                    {"level": "avoid", "max_per_100g": 20}],
    },
    note="新版营养标签配套更严阈值",
)


# ---- 顾客画像与建议引擎 ----------------------------------------------------

@dataclass(frozen=True)
class CustomerProfile:
    profile_id: str
    # 授权范围：只有列出的健康关注才允许参与计算
    scopes: frozenset[str]
    diabetes: bool = False
    gi_sensitive: bool = False
    allergens: frozenset[str] = field(default_factory=frozenset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "scopes": sorted(self.scopes),
            "diabetes": self.diabetes,
            "gi_sensitive": self.gi_sensitive,
            "allergens": sorted(self.allergens),
        }


def build_profile(raw: dict[str, Any]) -> CustomerProfile:
    scopes = frozenset(raw.get("scopes") or ())
    unknown = scopes - frozenset({"diabetes", "gi", "allergy", "general"})
    if unknown:
        raise ValueError(f"未知授权范围: {sorted(unknown)}")
    allergens = frozenset(raw.get("allergens") or ())
    bad = allergens - frozenset(KNOWN_ALLERGENS)
    if bad:
        raise ValueError(f"未知过敏原代码: {sorted(bad)}")
    return CustomerProfile(
        profile_id=str(raw["profile_id"]),
        scopes=scopes,
        diabetes=bool(raw.get("diabetes", False)),
        gi_sensitive=bool(raw.get("gi_sensitive", False)),
        allergens=allergens,
    )


@dataclass(frozen=True)
class Advice:
    eligible: bool                       # 是否可向该顾客推荐
    blocks: tuple[dict[str, str], ...]   # 阻断项（过敏原冲突）
    items: tuple[dict[str, str], ...]    # 非阻断提醒（每种成分至多一条）
    ignored_concerns: tuple[str, ...]    # 画像存在但未授权、未参与计算的关注
    label_version: str
    ingredient_version: str
    rule_version: str
    per_100g: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "blocks": list(self.blocks),
            "items": list(self.items),
            "ignored_concerns": list(self.ignored_concerns),
            "label_version": self.label_version,
            "ingredient_version": self.ingredient_version,
            "rule_version": self.rule_version,
            "per_100g": self.per_100g,
        }


def _active_concerns(profile: CustomerProfile | None) -> tuple[set[str], list[str]]:
    """返回 (授权且生效的关注, 被画像声明但因未授权而忽略的关注)。

    无画像（面向通用推荐）时按全部营养关注评估；过敏原关注必须由
    显式授权画像携带，绝不凭空参与。
    """
    if profile is None:
        return {"general", "diabetes", "gi"}, []
    active: set[str] = {"general"}
    ignored: list[str] = []
    declared = {"diabetes": profile.diabetes, "gi": profile.gi_sensitive,
                "allergy": bool(profile.allergens)}
    for concern, present in declared.items():
        if present and concern in profile.scopes:
            active.add(concern)
        elif present and concern not in profile.scopes:
            ignored.append(concern)
    return active, ignored


def evaluate(label: NormalizedLabel, rules: RuleSet,
             profile: CustomerProfile | None = None) -> Advice:
    """对一条已核验标签按指定规则版本与授权画像给出结论。

    过敏原冲突永远是阻断结论；阈值提醒按成分归并为单条消息，
    糖尿病与胃肠不适同时关注糖时只产生一条（取最严级别），避免互相矛盾。
    """
    if not label.valid:
        raise ValueError("无效标签不得参与推荐计算")

    active, ignored = _active_concerns(profile)

    blocks: list[dict[str, str]] = []
    if profile is not None and "allergy" in active:
        for code in sorted(label.allergens & profile.allergens):
            blocks.append({
                "code": f"allergen:{code}",
                "message": f"含过敏原{KNOWN_ALLERGENS[code]}，禁止向该顾客推荐/上架",
            })

    # nutrient -> {"level": 最严级别, "audiences": [关注名...]}
    merged: dict[str, dict[str, Any]] = {}
    for concern in active:
        if concern == "allergy":
            continue
        for nutrient in CONCERN_NUTRIENTS[concern]:
            value = label.per_100g.get(nutrient)
            if value is None:
                continue
            hit = rules.level_for(nutrient, value)
            if hit is None:
                continue
            slot = merged.setdefault(nutrient, {"level": "ok", "audiences": []})
            if LEVEL_ORDER[hit.level] > LEVEL_ORDER[slot["level"]]:
                slot["level"] = hit.level
                slot["threshold"] = hit
            if concern not in slot["audiences"]:
                slot["audiences"].append(concern)

    items: list[dict[str, str]] = []
    has_avoid = False
    for nutrient in ("energy_kcal", "fat_g", "sodium_mg", "sugar_g"):
        slot = merged.get(nutrient)
        if not slot:
            continue
        zh, unit = NUTRIENTS[nutrient]
        audiences = "、".join(CONCERN_NAME[c] for c in slot["audiences"])
        if slot["level"] == "avoid":
            has_avoid = True
        items.append({
            "code": f"{nutrient}:{slot['level']}",
            "concern": nutrient,
            "severity": slot["level"],
            "message": (f"{zh}每100克 {label.per_100g[nutrient]}{unit}，"
                        f"超过{rules.version}版{slot['threshold'].max_per_100g:g}{unit}阈值，"
                        f"{audiences}请{LEVEL_WORD[slot['level']]}"),
        })

    return Advice(
        eligible=not blocks and not has_avoid,
        blocks=tuple(blocks),
        items=tuple(items),
        ignored_concerns=tuple(ignored),
        label_version=label.label_version,
        ingredient_version=label.ingredient_version,
        rule_version=rules.version,
        per_100g=dict(label.per_100g),
    )
