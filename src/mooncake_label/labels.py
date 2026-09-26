"""标签核验服务：批量导入、规则版本、发布撤回与审计的持久化边界。"""
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from .domain import utc_now
from .nutrition import (
    CONCERN_SCOPES,
    DEFAULT_RULE_VERSION,
    DEFAULT_RULES,
    allergen_conflicts,
    evaluate_label,
    merge_rules,
    normalize_label,
)
from .service import ServiceError

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS labels(
 label_id TEXT PRIMARY KEY,
 batch_id TEXT NOT NULL,
 client_key TEXT NOT NULL,
 sku TEXT NOT NULL,
 origin TEXT,
 label_version TEXT NOT NULL,
 payload TEXT NOT NULL,
 normalized TEXT NOT NULL,
 issues TEXT NOT NULL,
 status TEXT NOT NULL,
 version INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(batch_id, client_key)
);
CREATE TABLE IF NOT EXISTS imports(
 batch_id TEXT PRIMARY KEY,
 fingerprint TEXT NOT NULL,
 result TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts(
 alert_id TEXT PRIMARY KEY,
 label_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 body TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(label_id, kind)
);
CREATE TABLE IF NOT EXISTS rules(
 version TEXT PRIMARY KEY,
 body TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles(
 profile_id TEXT PRIMARY KEY,
 tags TEXT NOT NULL,
 scopes TEXT NOT NULL,
 version INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations(
 eval_id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL,
 rule_version TEXT NOT NULL,
 request_key TEXT UNIQUE,
 results TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
 event_id TEXT PRIMARY KEY,
 entity TEXT NOT NULL,
 entity_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 body TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency(
 request_key TEXT PRIMARY KEY,
 result TEXT NOT NULL
);
"""

# 标签状态机：invalid 标签只能修正数据后重新导入，不能发布。
LABEL_STATES = {"valid": {"published"}, "published": {"withdrawn"}, "withdrawn": {"published"}, "invalid": set()}

_UNIT_ANOMALY_PREFIXES = ("bad_unit", "bad_basis", "bad_value", "implausible", "duplicate_nutrient")


class LabelService:
    """月饼营养标签的核验、发布与推荐服务。

    全部写入走 SQLite 事务并记录事件；使用文件库时重启后结果与审计记录保持一致。
    """

    def __init__(self, database=":memory:", clock=utc_now):
        self.clock = clock
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.executescript(SCHEMA)
            if self.connection.execute("SELECT COUNT(*) AS n FROM rules").fetchone()["n"] == 0:
                self._insert_rule(DEFAULT_RULE_VERSION, dict(DEFAULT_RULES))
                self.connection.execute("INSERT OR IGNORE INTO meta VALUES('active_rule', ?)", (DEFAULT_RULE_VERSION,))
            self.connection.commit()

    def close(self):
        self.connection.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def _event(self, entity, entity_id, kind, body):
        self.connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, entity, entity_id, kind, json.dumps(body, ensure_ascii=False), self.clock()),
        )

    def _alert(self, label_id, kind, body):
        # UNIQUE(label_id, kind)：重复导入或重复核验不会重复生成告警。
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)",
            (f"{label_id}:{kind}", label_id, kind, json.dumps(body, ensure_ascii=False), self.clock()),
        )
        return cursor.rowcount > 0

    def _insert_rule(self, version, body):
        self.connection.execute(
            "INSERT INTO rules VALUES(?,?,?)",
            (version, json.dumps(body, ensure_ascii=False, sort_keys=True), self.clock()),
        )
        self._event("rule", version, "rule_created", {"body": body})

    # ---- 批量导入 ----

    def import_batch(self, batch_id, items, request_key=None):
        """批量导入商品标签；批次号即幂等键，重复导入同一批返回首次结果，不重复生成告警。"""
        if not batch_id:
            raise ServiceError("批次号不能为空")
        if not isinstance(items, list) or not items:
            raise ServiceError("导入内容不能为空")
        fingerprint = hashlib.sha256(
            json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        with self._tx():
            cached = self.connection.execute("SELECT * FROM imports WHERE batch_id=?", (batch_id,)).fetchone()
            if cached is not None:
                if cached["fingerprint"] != fingerprint:
                    raise ServiceError("批次号已使用且内容不一致", "conflict")
                return json.loads(cached["result"])
            labels_out, alerts_out, seen_keys, skipped = [], [], set(), 0
            for item in items:
                if not isinstance(item, dict):
                    raise ServiceError("导入条目格式非法")
                sku, label_version = item.get("sku"), item.get("label_version")
                if not sku or not label_version:
                    raise ServiceError("导入条目缺少 sku 或 label_version")
                client_key = str(item.get("client_key") or f"{sku}:{label_version}")
                if client_key in seen_keys:
                    skipped += 1  # 批内重复条目去重
                    continue
                seen_keys.add(client_key)
                label_id = f"{batch_id}:{client_key}"
                normalized, issues = normalize_label(item.get("nutrients"))
                status = "valid" if not issues else "invalid"
                now = self.clock()
                self.connection.execute(
                    "INSERT INTO labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        label_id, batch_id, client_key, sku, item.get("origin"), str(label_version),
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                        json.dumps(normalized, sort_keys=True),
                        json.dumps(issues, ensure_ascii=False),
                        status, 1, now, now,
                    ),
                )
                self._event("label", label_id, "imported", {"batch_id": batch_id, "status": status, "issues": issues})
                if any(i.startswith("missing:") for i in issues):
                    if self._alert(label_id, "missing_nutrient", {"issues": issues}):
                        alerts_out.append({"label_id": label_id, "kind": "missing_nutrient"})
                if any(i.startswith(_UNIT_ANOMALY_PREFIXES) for i in issues):
                    if self._alert(label_id, "unit_anomaly", {"issues": issues}):
                        alerts_out.append({"label_id": label_id, "kind": "unit_anomaly"})
                undeclared, free_hit = allergen_conflicts(item)
                if undeclared or free_hit:
                    body = {"undeclared": undeclared, "free_from_conflict": free_hit}
                    if self._alert(label_id, "allergen_conflict", body):
                        alerts_out.append({"label_id": label_id, "kind": "allergen_conflict"})
                labels_out.append({"label_id": label_id, "sku": sku, "status": status, "issues": issues})
            result = {
                "batch_id": batch_id, "imported": len(labels_out), "duplicates_skipped": skipped,
                "labels": labels_out, "alerts": alerts_out,
            }
            self.connection.execute(
                "INSERT INTO imports VALUES(?,?,?,?)",
                (batch_id, fingerprint, json.dumps(result, ensure_ascii=False), self.clock()),
            )
            self._event("batch", batch_id, "batch_imported", {"imported": len(labels_out), "alerts": len(alerts_out)})
            return result

    # ---- 规则版本 ----

    def create_rules(self, version, body, activate=True):
        """新建规则版本；历史评估仍按各自记录的版本解释。"""
        if not version:
            raise ServiceError("规则版本号不能为空")
        try:
            merged = merge_rules(body)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        with self._tx():
            if self.connection.execute("SELECT 1 FROM rules WHERE version=?", (version,)).fetchone():
                raise ServiceError("规则版本已存在", "conflict")
            self._insert_rule(version, merged)
            if activate:
                self.connection.execute("INSERT OR REPLACE INTO meta VALUES('active_rule', ?)", (version,))
            return {"version": version, "rules": merged, "active": bool(activate)}

    def _rules_for(self, version=None):
        if version is None:
            version = self.connection.execute("SELECT value FROM meta WHERE key='active_rule'").fetchone()["value"]
        row = self.connection.execute("SELECT body FROM rules WHERE version=?", (version,)).fetchone()
        if row is None:
            raise ServiceError(f"规则版本不存在:{version}", "not_found")
        return version, json.loads(row["body"])

    def list_rules(self):
        with self._lock:
            active = self.connection.execute("SELECT value FROM meta WHERE key='active_rule'").fetchone()["value"]
            rows = self.connection.execute("SELECT version, body, created_at FROM rules ORDER BY rowid").fetchall()
            return {
                "active": active,
                "rules": [
                    {"version": r["version"], "rules": json.loads(r["body"]), "created_at": r["created_at"]}
                    for r in rows
                ],
            }

    def dry_run_rules(self, rule_body=None, rule_version=None, label_ids=None, tags=None, scopes=None):
        """规则试算：按候选规则或既有版本计算结论，不落库、不写事件。

        默认按全部关注点模拟（糖尿病/过敏/胃肠不适），可用 tags、scopes 覆盖。
        """
        with self._lock:
            if rule_body is not None:
                try:
                    rules = merge_rules(rule_body)
                except ValueError as exc:
                    raise ServiceError(str(exc)) from exc
                version = rule_version or "candidate"
            else:
                version, rules = self._rules_for(rule_version)
            tags = tags if tags is not None else {"diabetic": True, "gi_sensitive": True, "allergens": []}
            scopes = scopes if scopes is not None else list(CONCERN_SCOPES)
            wanted = set(label_ids) if label_ids else None
            results, excluded = [], []
            for row in self.connection.execute("SELECT * FROM labels ORDER BY rowid").fetchall():
                if wanted is not None and row["label_id"] not in wanted:
                    continue
                if row["status"] == "invalid":
                    excluded.append({
                        "label_id": row["label_id"], "issues": json.loads(row["issues"]),
                        "reason": "缺失或单位异常，不得进入推荐",
                    })
                    continue
                payload = json.loads(row["payload"])
                outcome = evaluate_label(
                    json.loads(row["normalized"]), payload.get("declared_allergens"), tags, scopes, rules
                )
                results.append({"label_id": row["label_id"], "sku": row["sku"], "status": row["status"], **outcome})
            return {"rule_version": version, "rules": rules, "results": results, "excluded": excluded}

    # ---- 发布与撤回 ----

    def publish_label(self, label_id, request_key):
        """发布标签；发现过敏原冲突时阻止发布并记录审计事件，而不是只返回提示。"""
        return self._transition_label(label_id, "published", request_key)

    def withdraw_label(self, label_id, request_key):
        return self._transition_label(label_id, "withdrawn", request_key)

    def _transition_label(self, label_id, target, request_key):
        if not request_key:
            raise ServiceError("缺少请求键")
        blocked, result = None, None
        with self._tx():
            cached = self.connection.execute(
                "SELECT result FROM idempotency WHERE request_key=?", (request_key,)
            ).fetchone()
            if cached is not None:
                return json.loads(cached["result"])
            row = self._label_row(label_id)
            if target not in LABEL_STATES.get(row["status"], set()):
                raise ServiceError(f"状态迁移不允许:{row['status']}→{target}", "conflict")
            if target == "published":
                undeclared, free_hit = allergen_conflicts(json.loads(row["payload"]))
                if undeclared or free_hit:
                    blocked = {"undeclared": undeclared, "free_from_conflict": free_hit}
                    self._alert(label_id, "allergen_conflict", blocked)
                    self._event("label", label_id, "publish_blocked", blocked)
            if blocked is None:
                version = row["version"] + 1
                now = self.clock()
                self.connection.execute(
                    "UPDATE labels SET status=?, version=?, updated_at=? WHERE label_id=?",
                    (target, version, now, label_id),
                )
                self._event("label", label_id, target, {"from": row["status"], "to": target, "version": version})
                result = {"label_id": label_id, "status": target, "version": version}
                self.connection.execute(
                    "INSERT INTO idempotency VALUES(?,?)", (request_key, json.dumps(result, ensure_ascii=False))
                )
        if blocked is not None:
            # 阻止记录已随事务提交，此处再抛错阻断发布流程。
            raise ServiceError(
                f"过敏原冲突，已阻止发布: 未申报{blocked['undeclared']} 与“不含”宣称冲突{blocked['free_from_conflict']}",
                "conflict",
            )
        return result

    # ---- 顾客画像 ----

    def upsert_profile(self, profile_id, tags, scopes):
        """登记顾客画像；scopes 是授权范围，未授权的画像标签不参与计算。"""
        if not profile_id:
            raise ServiceError("画像编号不能为空")
        scopes = sorted(set(scopes or []))
        unknown = sorted(set(scopes) - set(CONCERN_SCOPES))
        if unknown:
            raise ServiceError(f"未知授权范围:{unknown}")
        tags = tags or {}
        with self._tx():
            row = self.connection.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
            version = row["version"] + 1 if row else 1
            now = self.clock()
            self.connection.execute(
                "INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?,?)",
                (
                    profile_id, json.dumps(tags, ensure_ascii=False), json.dumps(scopes, ensure_ascii=False),
                    version, row["created_at"] if row else now, now,
                ),
            )
            self._event("profile", profile_id, "profile_upserted", {"version": version, "scopes": scopes})
            return {"profile_id": profile_id, "version": version, "scopes": scopes}

    def get_profile(self, profile_id):
        with self._lock:
            row = self.connection.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
            if row is None:
                raise ServiceError("画像不存在", "not_found")
            return {
                "profile_id": profile_id, "tags": json.loads(row["tags"]), "scopes": json.loads(row["scopes"]),
                "version": row["version"], "created_at": row["created_at"], "updated_at": row["updated_at"],
            }

    # ---- 推荐与历史解释 ----

    def recommend(self, profile_id, request_key, label_ids=None, rule_version=None):
        """为画像生成推荐；只纳入已发布且换算合格的标签，结果连同规则版本与快照落库。"""
        if not request_key:
            raise ServiceError("缺少请求键")
        with self._tx():
            cached = self.connection.execute(
                "SELECT results FROM evaluations WHERE request_key=?", (request_key,)
            ).fetchone()
            if cached is not None:
                return json.loads(cached["results"])
            profile = self.connection.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
            if profile is None:
                raise ServiceError("画像不存在", "not_found")
            version, rules = self._rules_for(rule_version)
            tags, scopes = json.loads(profile["tags"]), json.loads(profile["scopes"])
            wanted = set(label_ids) if label_ids else None
            results, excluded = [], []
            for row in self.connection.execute("SELECT * FROM labels ORDER BY rowid").fetchall():
                if wanted is not None and row["label_id"] not in wanted:
                    continue
                if row["status"] != "published":
                    if wanted is not None:
                        excluded.append({
                            "label_id": row["label_id"], "status": row["status"],
                            "reason": "未发布或换算不合格，不得进入推荐",
                        })
                    continue
                payload = json.loads(row["payload"])
                outcome = evaluate_label(
                    json.loads(row["normalized"]), payload.get("declared_allergens"), tags, scopes, rules
                )
                results.append({
                    "label_id": row["label_id"], "sku": row["sku"],
                    "label_version": row["label_version"], **outcome,
                })
            if wanted is not None:
                known = {r["label_id"] for r in results} | {e["label_id"] for e in excluded}
                for label_id in sorted(wanted - known):
                    excluded.append({"label_id": label_id, "status": "missing", "reason": "标签不存在"})
            eval_id = uuid.uuid4().hex
            record = {
                "eval_id": eval_id, "profile_id": profile_id, "rule_version": version, "rules": rules,
                "applied_scopes": scopes, "unauthorized_skipped": self._unauthorized_tags(tags, scopes),
                "results": results, "excluded": excluded, "created_at": self.clock(),
            }
            self.connection.execute(
                "INSERT INTO evaluations VALUES(?,?,?,?,?,?)",
                (eval_id, profile_id, version, request_key, json.dumps(record, ensure_ascii=False), record["created_at"]),
            )
            self._event("evaluation", eval_id, "evaluation_created",
                        {"profile_id": profile_id, "rule_version": version, "labels": len(results)})
            return record

    def get_evaluation(self, eval_id):
        """读取历史评估；结果与规则快照在写入时固定，规则更新后仍按当时版本解释。"""
        with self._lock:
            row = self.connection.execute("SELECT results FROM evaluations WHERE eval_id=?", (eval_id,)).fetchone()
            if row is None:
                raise ServiceError("评估记录不存在", "not_found")
            return json.loads(row["results"])

    @staticmethod
    def _unauthorized_tags(tags, scopes):
        skipped = []
        if tags.get("diabetic") and "diabetes" not in scopes:
            skipped.append("diabetes")
        if tags.get("allergens") and "allergy" not in scopes:
            skipped.append("allergy")
        if tags.get("gi_sensitive") and "gi" not in scopes:
            skipped.append("gi")
        return skipped

    # ---- 查询与审计 ----

    def _label_row(self, label_id):
        row = self.connection.execute("SELECT * FROM labels WHERE label_id=?", (label_id,)).fetchone()
        if row is None:
            raise ServiceError("标签不存在", "not_found")
        return row

    @staticmethod
    def _label_dict(row):
        return {
            "label_id": row["label_id"], "batch_id": row["batch_id"], "sku": row["sku"],
            "origin": row["origin"], "label_version": row["label_version"],
            "status": row["status"], "version": row["version"],
            "issues": json.loads(row["issues"]), "normalized": json.loads(row["normalized"]),
            "payload": json.loads(row["payload"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def get_label(self, label_id):
        with self._lock:
            return self._label_dict(self._label_row(label_id))

    def list_labels(self, status=None):
        with self._lock:
            if status:
                rows = self.connection.execute("SELECT * FROM labels WHERE status=? ORDER BY rowid", (status,)).fetchall()
            else:
                rows = self.connection.execute("SELECT * FROM labels ORDER BY rowid").fetchall()
            return [self._label_dict(r) for r in rows]

    def list_alerts(self, label_id=None):
        with self._lock:
            if label_id:
                rows = self.connection.execute("SELECT * FROM alerts WHERE label_id=? ORDER BY rowid", (label_id,)).fetchall()
            else:
                rows = self.connection.execute("SELECT * FROM alerts ORDER BY rowid").fetchall()
            return [
                {"alert_id": r["alert_id"], "label_id": r["label_id"], "kind": r["kind"],
                 "body": json.loads(r["body"]), "created_at": r["created_at"]}
                for r in rows
            ]

    def label_events(self, label_id):
        return self.list_events("label", label_id)

    def list_events(self, entity=None, entity_id=None):
        with self._lock:
            sql, clauses, params = "SELECT * FROM events", [], []
            if entity:
                clauses.append("entity=?")
                params.append(entity)
            if entity_id:
                clauses.append("entity_id=?")
                params.append(entity_id)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            rows = self.connection.execute(sql + " ORDER BY rowid", params).fetchall()
            return [
                {"event_id": r["event_id"], "entity": r["entity"], "entity_id": r["entity_id"],
                 "kind": r["kind"], "body": json.loads(r["body"]), "created_at": r["created_at"]}
                for r in rows
            ]
