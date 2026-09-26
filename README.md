# 月饼营养核验库

面向连锁食品门店中秋集中上架场景的标签核验服务：不同产地、新旧版本的营养标签统一按每 100 克换算，按版本化规则为糖尿病、过敏、胃肠不适顾客给出不互相矛盾的提醒。核心写入使用 SQLite 事务，服务接口保持无页面依赖，便于运营人员在现场或后台系统中核对状态。

## 行为约定

- **统一换算**：能量统一为 kcal（支持 kJ 输入），脂肪/糖为 g、钠为 mg，每份标注按份量克数折算到每 100 克；缺失成分、异常单位、超合理范围的数值判为无效标签。
- **版本可追溯**：原料表与营养表版本随标签存储，换算结果、建议、订单都记录所用版本。
- **不得进入推荐**：无效标签在试算、发布、下单环节一律被拒绝。
- **告警去重**：同一批次重复导入整单回放；不同批次重复导入同一商品版本，告警靠唯一约束兜底不重复生成。
- **授权画像**：画像声明的健康关注只有在 `scopes` 授权范围内才参与计算，未授权的关注在结果中显式列为 `ignored_concerns`。
- **过敏原阻断**：发布前对全部授权画像核验，任一冲突即整体阻止发布（HTTP 422），并留下 `release.blocked` 审计。
- **不矛盾提醒**：同一成分只出一条提醒，取最严级别；糖尿病与胃肠不适同时关注糖时合并为一条。
- **历史钉版**：订单钉住下单时的规则版本与画像快照，规则更新后 `GET /orders/{id}` 仍按当时版本解释并校验快照一致。
- **重启一致**：默认文件数据库（`labels.db`），结果与审计记录重启后保持一致。

## 接口

    POST /imports                  批量导入 {"labels":[...], "batch_id"?, "request_key"?}
    POST /trials                   规则试算 {"product_ids"?, "profile_ids"?, "rule_version"? | "thresholds"?}
    POST /releases                 发布 {"release_id", "product_ids"?, "profile_ids"?, "rule_version"?}
    POST /releases/{id}/withdraw   撤回
    POST /orders                   下单 {"order_id","product_id","profile_id"?}
    POST /profiles                 登记画像 {"profile_id","scopes":[...],"diabetes"?,"gi_sensitive"?,"allergens"?}
    POST /rules                    注册新规则版本（自动生效，旧版本保留）
    GET  /products/{id}            商品全部标签版本
    GET  /releases/{id}            发布单
    GET  /orders/{id}              订单按钉住版本重算并比对快照
    GET  /alerts[?batch_id=]       告警
    GET  /audit[?entity_type=&entity_id=]  审计事件

## 目录

- src/mooncake_label/domain.py：换算、校验、规则集、建议引擎（纯计算）。
- src/mooncake_label/service.py：事务、导入/发布/撤回/订单、幂等、审计。
- src/mooncake_label/api.py：本地 HTTP 接口。
- tests/：换算、去重、阻断、钉版、授权、重启一致性测试。

## 运行

    PYTHONPATH=src python3 -m mooncake_label.api   # 127.0.0.1:8080，数据落 labels.db

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
