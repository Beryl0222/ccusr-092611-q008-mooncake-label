# 月饼营养核验库

这是面向月饼营养核验库的本地服务基础，负责保存业务记录、状态变化和可追溯事件。核心写入使用 SQLite 事务，服务接口保持无页面依赖，便于运营人员在现场或后台系统中核对状态。

## 能力

- 批量导入：批次号即幂等键，重复导入同一批商品返回首次结果，不重复生成告警；同批次号不同内容按冲突拒绝。
- 每百克换算：能量/脂肪/钠/糖统一换算到每百克目标单位（kJ、g、mg、g），支持每份折算与 kcal↔kJ；缺失或单位异常的标签标记为 invalid、生成去重告警，且不得进入推荐。
- 规则版本：阈值规则按版本保存，新建版本不影响历史评估；`POST /rules/dry-run` 试算不落库。
- 发布与撤回：发布前比对原料隐含过敏原与标签申报，发现冲突（未申报、与“不含”宣称矛盾）时阻止发布并记录审计事件，而不是只返回提示。
- 顾客画像：画像标签只在授权范围（diabetes/allergy/gi）内参与计算；每个关注点只产生一个结论，提醒文案由结论派生，不会互相矛盾。
- 历史解释：推荐结果落库时记录规则版本与规则快照，规则更新后历史订单仍按当时版本解释；使用文件库时重启后结果与审计记录一致。

## 目录

- src/mooncake_label/domain.py：领域对象与时间约定。
- src/mooncake_label/nutrition.py：每百克换算、过敏原比对与规则评估（纯函数）。
- src/mooncake_label/labels.py：导入、规则、发布、画像与评估的事务边界。
- src/mooncake_label/service.py：通用记录内核（事务、状态迁移、权限和幂等）。
- src/mooncake_label/api.py：本地 HTTP 接口。
- tests/：状态、版本、权限、换算、发布阻断和重启一致性测试。

## 接口

- `POST /batches/import`：批量导入（batch_id 幂等）。
- `POST /rules`、`GET /rules`、`POST /rules/dry-run`：规则版本与试算。
- `POST /labels/{id}/publish`、`POST /labels/{id}/withdraw`、`GET /labels/{id}`、`GET /labels/{id}/events`。
- `POST /profiles`、`GET /profiles/{id}`：顾客画像与授权范围。
- `POST /evaluations`、`GET /evaluations/{id}`：推荐与历史解释。
- `GET /alerts`、`GET /audit`：告警与审计事件。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
