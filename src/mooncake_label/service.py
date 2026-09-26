"""标签核验服务的持久化边界与业务操作。

所有状态变化（导入、发布、撤回、下单、画像/规则变更）都在 SQLite 事务内
连同审计事件一起提交，事务失败整体回滚；文件数据库在进程重启后仍可读出
一致的结果与审计记录。

关键边界：
- 批量导入支持 batch_id 去重，告警表还有唯一约束兜底，重复导入不重复告警；
- 发布前对授权画像做过敏原冲突核验，冲突则整体阻止发布（不是提示）；
- 订单钉住发布时的规则版本，规则更新后历史订单仍按当时版本解释；
- request_key 提供操作级幂等。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterable

from .domain import (
    Advice,
    CustomerProfile,
    DEFAULT_RULES_V1,
    Issue,
    NormalizedLabel,
    RuleSet,
    build_profile,
    build_ruleset,
    evaluate,
    normalize_label,
    utc_now,
)
from .domain import Record  # noqa: F401  (再导出，兼容旧调用方)


class ServiceError(Exception):
    """业务规则错误（HTTP 层映射为 400）。"""


class PublishBlocked(ServiceError):
    """发布被阻断，例如过敏原冲突（HTTP 层映射为 422）。"""

    def __init__(self, conflicts: list[dict[str, str]], message: str = "发布被阻断",
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.conflicts = conflicts
        self.details = details or {}


# ---- 旧版通用记录服务（保留以兼容既有状态机测试与调用方） ------------------

class DomainStore:
    def __init__(self, database=":memory:", clock=utc_now):
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self.connection.executescript(
            """PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS records(record_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,record_id TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(record_id) REFERENCES records(record_id));
CREATE TABLE IF NOT EXISTS idempotency(request_key TEXT PRIMARY KEY,result TEXT NOT NULL);"""
        )
        self.connection.commit()

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def create(self, record_id, owner_id, payload=None):
        with self.transaction():
            self.connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (record_id, owner_id, "draft", 1,
                 json.dumps(payload or {}), self.clock()))
            self.connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)",
                (record_id + ":created", record_id, "created", "{}", self.clock()))
        return self.get(record_id)

    def get(self, record_id):
        row = self.connection.execute(
            "SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise ServiceError("记录不存在")
        return Record(row["record_id"], row["owner_id"], row["state"],
                      row["version"], row["updated_at"])

    def transition(self, record_id, owner_id, target, request_key, expected_version=None):
        with self.transaction():
            old = self.connection.execute(
                "SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
            if old is None:
                raise ServiceError("记录不存在")
            if old["owner_id"] != owner_id:
                raise ServiceError("无权操作")
            cached = self.connection.execute(
                "SELECT result FROM idempotency WHERE request_key=?",
                (request_key,)).fetchone()
            if cached:
                return json.loads(cached["result"])
            if expected_version is not None and old["version"] != expected_version:
                raise ServiceError("版本冲突")
            allowed = {"draft": {"pending"}, "pending": {"approved", "cancelled"},
                       "approved": {"closed"}, "cancelled": set(), "closed": set()}
            if target not in allowed.get(old["state"], set()):
                raise ServiceError("状态迁移不允许")
            version = old["version"] + 1
            now = self.clock()
            self.connection.execute(
                "UPDATE records SET state=?,version=?,updated_at=? WHERE record_id=?",
                (target, version, now, record_id))
            body = json.dumps({"from": old["state"], "to": target, "version": version})
            self.connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)",
                (request_key + ":event", record_id, "transition", body, now))
            result = {"record_id": record_id, "state": target, "version": version}
            self.connection.execute(
                "INSERT INTO idempotency VALUES(?,?)", (request_key, json.dumps(result)))
            return result

    def close(self):
        self.connection.close()


# ---- 标签核验服务 ----------------------------------------------------------

_SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS labels(
  product_id TEXT NOT NULL,
  label_version TEXT NOT NULL,
  ingredient_version TEXT NOT NULL,
  origin TEXT NOT NULL,
  raw TEXT NOT NULL,
  normalized TEXT NOT NULL,
  valid INTEGER NOT NULL,
  batch_id TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(product_id, label_version)
);
CREATE TABLE IF NOT EXISTS batches(
  batch_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  product_count INTEGER NOT NULL,
  invalid_count INTEGER NOT NULL,
  alerts_new INTEGER NOT NULL,
  summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts(
  alert_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  label_version TEXT NOT NULL,
  code TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(product_id, label_version, code)
);
CREATE TABLE IF NOT EXISTS rules(
  version TEXT PRIMARY KEY,
  definition TEXT NOT NULL,
  active INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles(
  profile_id TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases(
  release_id TEXT PRIMARY KEY,
  rule_version TEXT NOT NULL,
  status TEXT NOT NULL,            -- published / withdrawn
  created_at TEXT NOT NULL,
  published_at TEXT,
  withdrawn_at TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS release_products(
  release_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  label_version TEXT NOT NULL,
  ingredient_version TEXT NOT NULL,
  PRIMARY KEY(release_id, product_id)
);
CREATE TABLE IF NOT EXISTS orders(
  order_id TEXT PRIMARY KEY,
  release_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  label_version TEXT NOT NULL,
  ingredient_version TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  profile_id TEXT,
  snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency(
  request_key TEXT PRIMARY KEY,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit(
  event_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  body TEXT NOT NULL
);
"""


def _label_from_row(row: sqlite3.Row | dict[str, Any]) -> NormalizedLabel:
    data = json.loads(row["normalized"])
    return NormalizedLabel(
        product_id=data["product_id"],
        label_version=data["label_version"],
        ingredient_version=data["ingredient_version"],
        origin=data["origin"],
        basis=data["basis"],
        per_100g=data["per_100g"],
        valid=data["valid"],
        issues=tuple(Issue(i["code"], i["message"]) for i in data["issues"]),
        allergens=frozenset(data["allergens"]),
    )


def _ruleset_from_dict(data: dict[str, Any]) -> RuleSet:
    return build_ruleset(
        data["version"],
        {n: [{"level": t["level"], "max_per_100g": t["max_per_100g"]}
             for t in ts]
         for n, ts in data["thresholds"].items()},
        note=data.get("note", ""),
    )


class LabelService:
    def __init__(self, database: str = ":memory:", clock=utc_now, actor: str = "nutritionist"):
        # check_same_thread=False：HTTP 线程可用；写入经 BEGIN IMMEDIATE 串行化
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self.actor = actor
        self.connection.executescript(_SCHEMA)
        self.connection.commit()
        self._seed_default_rules()

    def close(self) -> None:
        self.connection.close()

    # -- 基础设施 -----------------------------------------------------------

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _audit(self, action: str, entity_type: str, entity_id: str,
               body: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO audit VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, self.clock(), self.actor, action, entity_type,
             entity_id, json.dumps(body, ensure_ascii=False, sort_keys=True)))

    def _idempotent(self, namespace: str, request_key: str | None,
                    fn: Callable[[], Any]) -> Any:
        """在当前事务内做请求级幂等：同 key 直接回放首次结果。"""
        if not request_key:
            return fn()
        key = f"{namespace}:{request_key}"
        cached = self.connection.execute(
            "SELECT result FROM idempotency WHERE request_key=?", (key,)).fetchone()
        if cached:
            return json.loads(cached["result"])
        result = fn()
        self.connection.execute(
            "INSERT INTO idempotency VALUES(?,?,?)",
            (key, json.dumps(result, ensure_ascii=False, sort_keys=True), self.clock()))
        return result

    def _seed_default_rules(self) -> None:
        with self.transaction():
            empty = self.connection.execute("SELECT 1 FROM rules LIMIT 1").fetchone()
            if empty is None:
                self._register_ruleset(DEFAULT_RULES_V1)

    # -- 规则集 -------------------------------------------------------------

    def _register_ruleset(self, rules: RuleSet) -> None:
        # 新版本生效，旧版本保留（历史订单钉版解释仍可读取）
        self.connection.execute("UPDATE rules SET active=0")
        self.connection.execute(
            "INSERT OR REPLACE INTO rules VALUES(?,?,?,?)",
            (rules.version, json.dumps(rules.to_dict(), ensure_ascii=False, sort_keys=True),
             1, self.clock()))

    def register_ruleset(self, version: str, thresholds: dict[str, list[dict[str, Any]]],
                         note: str = "", request_key: str | None = None) -> dict[str, Any]:
        rules = build_ruleset(version, thresholds, note)  # 非法定义先在库外失败
        with self.transaction():
            def work():
                self._register_ruleset(rules)
                self._audit("rules.register", "rules", rules.version, rules.to_dict())
                return {"version": rules.version, "active": True, "note": rules.note}
            return self._idempotent("rules", request_key, work)

    def get_ruleset(self, version: str | None = None) -> RuleSet:
        if version is not None:
            row = self.connection.execute(
                "SELECT definition FROM rules WHERE version=?", (version,)).fetchone()
            if row is None:
                raise ServiceError(f"规则版本不存在: {version}")
            return _ruleset_from_dict(json.loads(row["definition"]))
        row = self.connection.execute(
            "SELECT definition FROM rules WHERE active=1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ServiceError("尚无生效规则版本")
        return _ruleset_from_dict(json.loads(row["definition"]))

    # -- 画像 ---------------------------------------------------------------

    def create_profile(self, raw: dict[str, Any],
                       request_key: str | None = None) -> dict[str, Any]:
        profile = build_profile(raw)  # 授权范围/过敏原代码非法即拒绝
        with self.transaction():
            def work():
                self.connection.execute(
                    "INSERT OR REPLACE INTO profiles VALUES(?,?,?)",
                    (profile.profile_id,
                     json.dumps(profile.to_dict(), ensure_ascii=False, sort_keys=True),
                     self.clock()))
                self._audit("profile.upsert", "profile", profile.profile_id,
                            profile.to_dict())
                return profile.to_dict()
            return self._idempotent("profile", request_key, work)

    def _get_profile(self, profile_id: str) -> CustomerProfile:
        row = self.connection.execute(
            "SELECT body FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if row is None:
            raise ServiceError(f"画像不存在: {profile_id}")
        return build_profile(json.loads(row["body"]))

    # -- 批量导入 -----------------------------------------------------------

    def import_labels(self, labels: list[dict[str, Any]], batch_id: str | None = None,
                      request_key: str | None = None) -> dict[str, Any]:
        if not isinstance(labels, list) or not labels:
            raise ServiceError("导入批次为空")
        batch_id = batch_id or uuid.uuid4().hex
        with self.transaction():
            # 同一批次重复导入：整单回放，不重新生成任何告警
            prior = self.connection.execute(
                "SELECT summary FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if prior:
                return json.loads(prior["summary"])

            def work():
                return self._do_import(labels, batch_id)
            return self._idempotent("import", request_key, work)

    def _do_import(self, labels: list[dict[str, Any]], batch_id: str) -> dict[str, Any]:
        now = self.clock()
        products: list[dict[str, Any]] = []
        inserted = 0
        invalid_count = 0
        alerts_new = 0
        for raw in labels:
            norm = normalize_label(raw)
            existing = self.connection.execute(
                "SELECT 1 FROM labels WHERE product_id=? AND label_version=?",
                (norm.product_id, norm.label_version)).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO labels VALUES(?,?,?,?,?,?,?,?,?)",
                    (norm.product_id, norm.label_version, norm.ingredient_version,
                     norm.origin, json.dumps(raw, ensure_ascii=False, sort_keys=True),
                     json.dumps(norm.to_dict(), ensure_ascii=False, sort_keys=True),
                     1 if norm.valid else 0, batch_id, now))
                inserted += 1
            # 无论新旧版本记录，异常都落告警；唯一约束保证同商品同版本同问题不重复
            for issue in norm.issues:
                alert_id = f"{norm.product_id}:{norm.label_version}:{issue.code}"
                cur = self.connection.execute(
                    "INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?,?,?)",
                    (alert_id, batch_id, norm.product_id, norm.label_version,
                     issue.code, issue.message, now))
                alerts_new += cur.rowcount
            if not norm.valid:
                invalid_count += 1
            products.append(norm.to_dict())

        summary = {
            "batch_id": batch_id,
            "received": len(labels),
            "labels_inserted": inserted,
            "invalid_count": invalid_count,
            "alerts_new": alerts_new,
            "recommendable": sorted(p["product_id"] for p in products if p["valid"]),
            "products": products,
        }
        self.connection.execute(
            "INSERT INTO batches VALUES(?,?,?,?,?,?)",
            (batch_id, now, len(labels), invalid_count, alerts_new,
             json.dumps(summary, ensure_ascii=False, sort_keys=True)))
        self._audit("labels.import", "batch", batch_id,
                    {"received": len(labels), "inserted": inserted,
                     "invalid": invalid_count, "alerts_new": alerts_new})
        return summary

    def list_alerts(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        if batch_id:
            rows = self.connection.execute(
                "SELECT * FROM alerts WHERE batch_id=? ORDER BY created_at, alert_id",
                (batch_id,)).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM alerts ORDER BY created_at, alert_id").fetchall()
        return [dict(r) for r in rows]

    def get_product(self, product_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM labels WHERE product_id=? ORDER BY rowid",
            (product_id,)).fetchall()
        if not rows:
            raise ServiceError(f"商品不存在: {product_id}")
        return {
            "product_id": product_id,
            "versions": [
                {
                    "label_version": r["label_version"],
                    "ingredient_version": r["ingredient_version"],
                    "origin": r["origin"],
                    "valid": bool(r["valid"]),
                    "batch_id": r["batch_id"],
                    "updated_at": r["updated_at"],
                    "normalized": json.loads(r["normalized"]),
                }
                for r in rows
            ],
        }

    def _latest_label(self, product_id: str) -> NormalizedLabel | None:
        row = self.connection.execute(
            "SELECT normalized FROM labels WHERE product_id=? AND valid=1 "
            "ORDER BY rowid DESC LIMIT 1", (product_id,)).fetchone()
        return _label_from_row(row) if row else None

    # -- 规则试算（只读，不落业务状态；留审计便于追溯谁试了什么） ------------

    def trial(self, product_ids: Iterable[str] | None = None,
              profile_ids: Iterable[str] | None = None,
              rule_version: str | None = None,
              thresholds: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
        if thresholds is not None:
            rules = build_ruleset(f"trial@{uuid.uuid4().hex[:8]}", thresholds,
                                  note="试算临时规则")
            rule_version = rules.version
        else:
            rules = self.get_ruleset(rule_version)
        profiles = [self._get_profile(pid) for pid in (profile_ids or [])]
        products = self._trial_products(product_ids)
        results = [self._advices_for(label, rules, profiles) for label in products]
        body = {"rule_version": rules.version,
                "products": [r["product_id"] for r in results],
                "profiles": [p.profile_id for p in profiles],
                "results": results}
        with self.transaction():
            self._audit("rules.trial", "rules", rules.version,
                        {"products": body["products"], "profiles": body["profiles"]})
        return body

    def _trial_products(self, product_ids: Iterable[str] | None) -> list[NormalizedLabel]:
        if product_ids is None:
            rows = self.connection.execute(
                "SELECT normalized FROM labels WHERE valid=1").fetchall()
            labels = [_label_from_row(r) for r in rows]
        else:
            labels = []
            for pid in product_ids:
                label = self._latest_label(pid)
                if label is None:
                    raise ServiceError(f"商品无有效标签，不得进入试算/推荐: {pid}")
                labels.append(label)
        labels.sort(key=lambda l: l.product_id)
        return labels

    @staticmethod
    def _advices_for(label: NormalizedLabel, rules: RuleSet,
                     profiles: list[CustomerProfile]) -> dict[str, Any]:
        advices: dict[str, Any] = {
            "product_id": label.product_id,
            "label_version": label.label_version,
            "ingredient_version": label.ingredient_version,
            "origin": label.origin,
            "per_100g": label.per_100g,
            "general": evaluate(label, rules).to_dict(),
            "profiles": {},
        }
        for p in profiles:
            advices["profiles"][p.profile_id] = evaluate(label, rules, p).to_dict()
        return advices

    # -- 发布 / 撤回 --------------------------------------------------------

    def publish(self, release_id: str, product_ids: list[str] | None = None,
                profile_ids: list[str] | None = None, rule_version: str | None = None,
                note: str = "", request_key: str | None = None) -> dict[str, Any]:
        try:
            with self.transaction():
                def work():
                    return self._do_publish(release_id, product_ids, profile_ids,
                                            rule_version, note)
                return self._idempotent("release", request_key, work)
        except PublishBlocked as blocked:
            # 阻断本身必须留痕；业务写入已随事务回滚，审计单独提交
            with self.transaction():
                self._audit("release.blocked", "release", release_id,
                            {**blocked.details, "conflicts": blocked.conflicts})
            raise

    def _do_publish(self, release_id: str, product_ids: list[str] | None,
                    profile_ids: list[str] | None, rule_version: str | None,
                    note: str) -> dict[str, Any]:
        if self.connection.execute(
                "SELECT 1 FROM releases WHERE release_id=?", (release_id,)).fetchone():
            raise ServiceError(f"发布单已存在: {release_id}")
        rules = self.get_ruleset(rule_version)
        profiles = [self._get_profile(pid) for pid in (profile_ids or [])]

        if product_ids is None:
            rows = self.connection.execute(
                "SELECT normalized FROM labels WHERE valid=1").fetchall()
            labels = sorted((_label_from_row(r) for r in rows),
                            key=lambda l: l.product_id)
        else:
            labels = []
            invalid_requested = []
            for pid in product_ids:
                label = self._latest_label(pid)
                if label is None:
                    invalid_requested.append(pid)
                else:
                    labels.append(label)
            if invalid_requested:
                raise ServiceError(
                    f"以下商品缺失或标签单位异常，不得进入发布推荐: {invalid_requested}")

        # 过敏原冲突：对全部授权画像核验，任一冲突即整体阻止发布
        conflicts: list[dict[str, str]] = []
        for label in labels:
            for p in profiles:
                advice = evaluate(label, rules, p)
                for block in advice.blocks:
                    conflicts.append({
                        "product_id": label.product_id,
                        "label_version": label.label_version,
                        "profile_id": p.profile_id,
                        "code": block["code"],
                        "message": block["message"],
                    })
        if conflicts:
            raise PublishBlocked(
                conflicts,
                f"过敏原冲突，发布已阻止（{len(conflicts)} 项）",
                details={"release_id": release_id, "rule_version": rules.version,
                         "status": "blocked"})

        now = self.clock()
        self.connection.execute(
            "INSERT INTO releases VALUES(?,?,?,?,?,?,?)",
            (release_id, rules.version, "published", now, now, None, note))
        results = []
        for label in labels:
            self.connection.execute(
                "INSERT INTO release_products VALUES(?,?,?,?)",
                (release_id, label.product_id, label.label_version,
                 label.ingredient_version))
            results.append(self._advices_for(label, rules, profiles))
        body = {
            "release_id": release_id,
            "status": "published",
            "rule_version": rules.version,
            "product_count": len(labels),
            "products": [r["product_id"] for r in results],
            "results": results,
            "published_at": now,
            "note": note,
        }
        self._audit("release.publish", "release", release_id,
                    {"rule_version": rules.version,
                     "products": body["products"], "profiles": [p.profile_id for p in profiles]})
        return body

    def get_release(self, release_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
        if row is None:
            raise ServiceError(f"发布单不存在: {release_id}")
        products = self.connection.execute(
            "SELECT product_id,label_version,ingredient_version FROM release_products "
            "WHERE release_id=? ORDER BY product_id", (release_id,)).fetchall()
        return {
            "release_id": release_id,
            "rule_version": row["rule_version"],
            "status": row["status"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "withdrawn_at": row["withdrawn_at"],
            "note": row["note"],
            "products": [dict(r) for r in products],
        }

    def withdraw(self, release_id: str, request_key: str | None = None) -> dict[str, Any]:
        with self.transaction():
            def work():
                row = self.connection.execute(
                    "SELECT * FROM releases WHERE release_id=?",
                    (release_id,)).fetchone()
                if row is None:
                    raise ServiceError(f"发布单不存在: {release_id}")
                if row["status"] == "withdrawn":
                    return {"release_id": release_id, "status": "withdrawn",
                            "withdrawn_at": row["withdrawn_at"]}
                if row["status"] != "published":
                    raise ServiceError(f"当前状态 {row['status']} 不可撤回")
                now = self.clock()
                self.connection.execute(
                    "UPDATE releases SET status='withdrawn', withdrawn_at=? "
                    "WHERE release_id=?", (now, release_id))
                self._audit("release.withdraw", "release", release_id,
                            {"previous_rule_version": row["rule_version"]})
                return {"release_id": release_id, "status": "withdrawn",
                        "withdrawn_at": now}
            return self._idempotent("withdraw", request_key, work)

    def _active_release_for(self, product_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT r.* FROM releases r JOIN release_products rp "
            "ON rp.release_id=r.release_id "
            "WHERE r.status='published' AND rp.product_id=? "
            "ORDER BY r.published_at DESC LIMIT 1", (product_id,)).fetchone()
        if row is None:
            raise ServiceError(f"商品未处于已发布清单，不能接单: {product_id}")
        return row

    # -- 订单（历史钉版） ----------------------------------------------------

    def create_order(self, order_id: str, product_id: str,
                     profile_id: str | None = None,
                     request_key: str | None = None) -> dict[str, Any]:
        with self.transaction():
            def work():
                return self._do_create_order(order_id, product_id, profile_id)
            return self._idempotent("order", request_key, work)

    def _do_create_order(self, order_id: str, product_id: str,
                         profile_id: str | None) -> dict[str, Any]:
        if self.connection.execute(
                "SELECT 1 FROM orders WHERE order_id=?", (order_id,)).fetchone():
            raise ServiceError(f"订单已存在: {order_id}")
        release = self._active_release_for(product_id)
        label_version = self.connection.execute(
            "SELECT label_version,ingredient_version FROM release_products "
            "WHERE release_id=? AND product_id=?",
            (release["release_id"], product_id)).fetchone()
        row = self.connection.execute(
            "SELECT normalized FROM labels WHERE product_id=? AND label_version=?",
            (product_id, label_version["label_version"])).fetchone()
        label = _label_from_row(row)
        rules = self.get_ruleset(release["rule_version"])  # 钉住下单时版本
        profile = self._get_profile(profile_id) if profile_id else None
        advice = evaluate(label, rules, profile)

        now = self.clock()
        snapshot = {
            "rule_version": rules.version,
            "label_version": label.label_version,
            "ingredient_version": label.ingredient_version,
            "profile": profile.to_dict() if profile else None,
            "advice": advice.to_dict(),
        }
        self.connection.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
            (order_id, release["release_id"], product_id, label.label_version,
             label.ingredient_version, rules.version, profile_id,
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now))
        self._audit("order.create", "order", order_id,
                    {"product_id": product_id, "rule_version": rules.version,
                     "label_version": label.label_version, "profile_id": profile_id,
                     "eligible": advice.eligible})
        return {"order_id": order_id, "product_id": product_id,
                "release_id": release["release_id"], **snapshot, "created_at": now}

    def get_order(self, order_id: str) -> dict[str, Any]:
        """按订单钉住的规则版本重新解释；规则后续更新不影响历史结论。"""
        row = self.connection.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise ServiceError(f"订单不存在: {order_id}")
        label_row = self.connection.execute(
            "SELECT normalized FROM labels WHERE product_id=? AND label_version=?",
            (row["product_id"], row["label_version"])).fetchone()
        rules = self.get_ruleset(row["rule_version"])
        stored = json.loads(row["snapshot"])
        # 使用下单时保存的画像快照，画像后续变更也不改变历史解释
        profile = build_profile(stored["profile"]) if stored.get("profile") else None
        label = _label_from_row(label_row)
        advice = evaluate(label, rules, profile)
        return {
            "order_id": order_id,
            "product_id": row["product_id"],
            "release_id": row["release_id"],
            "created_at": row["created_at"],
            "pinned": {
                "rule_version": row["rule_version"],
                "label_version": row["label_version"],
                "ingredient_version": row["ingredient_version"],
                "profile_id": row["profile_id"],
            },
            "stored_snapshot": json.loads(row["snapshot"]),
            "recomputed_under_pinned_rules": advice.to_dict(),
            "consistent": json.loads(row["snapshot"])["advice"] == advice.to_dict(),
        }

    # -- 审计 ---------------------------------------------------------------

    def audit_tail(self, entity_type: str | None = None,
                   entity_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit WHERE 1=1"
        args: list[Any] = []
        if entity_type:
            sql += " AND entity_type=?"
            args.append(entity_type)
        if entity_id:
            sql += " AND entity_id=?"
            args.append(entity_id)
        sql += " ORDER BY rowid DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.connection.execute(sql, args).fetchall()]
