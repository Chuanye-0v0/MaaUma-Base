# 继承工具数据与 API

`hub/parents.py` 为本候选内自包含标准库模块，不导入 Qt／JVM，也不依赖真实 MDB 或现存 MaaUma 账号目录。默认数据只读；仅显式 `inventory.import/save` 才写候选的 `data/user-libraries`，可用 `storage_root=` 注入独立测试目录。

## 数据来源

三服关系是研究目录 `normalized/relations-{cn,tw,jp}.json` 的逐字复制：

| 服 | 有名角色 | 关系组 | 原始 wiki 修订 |
| --- | ---: | ---: | --- |
| 国服 | 96 | 1891 | relation 146103 / chara 146104（2026-09-06） |
| 繁中服 | 109 | 2442 | relation 138170 / chara 138171（2026-01-02） |
| 日服 | 135 | 3258 | relation 146454 / chara 146455（2026-09-20） |

出处字段、原始 SHA、未命名引用角色和分发审查状态仍在 JSON 内。它们是**分服社区快照**，不是宣称已与当日游戏逐项校准；译名字段保留 `community_wiki_reference`，不能改称官方当前译名。

`race-catalog-reference.json` 从已发布 0.4.38 本地目录定向复制，401 条窗口记录，来源 `cn_standard_v1`，生成于 2026-08-09；名称和赛程仍是参考。JP／TW 默认不会静默套用这份 CN 日历，仍会列共同 G1 候选。显式 `allow_reference_calendar=true` 或提供自己的 `races` 才产生相应参考日期。

关系交集、确定性红星和已有／待培养分离的实现沿用本项目 0.4.38 的独立数学契约。可选概率模型参考 [UmaLegacyLinker 的固定评分说明](https://github.com/Rom1foucher/UmaLegacyLinker/blob/c8df8fc953c041c4a194e2ec488cbfe6ab2d21c4/docs/SCORING.md)，以新写的标准数学运算实现，未引入其 Python 源码。概率及每共同 G1 +3 都不能据此称为国服已验证机制。外部仓库原许可证保留在研究目录，不用进程边界来声称免除许可证义务。

## 请求与输出

```python
from hub import parents
parents.dispatch({"action":"parents.evaluate","server":"cn","nodes":{
  "target":1001,"parent_a":1002,"parent_b":1003,
  "gp_a1":1004,"gp_a2":1005,"gp_b1":1006,"gp_b2":1007
}})
```

所有节点使用稳定 `character_id`；`outfit_id/variant_id`、`entry_id` 单独保存。节点既可为数字，也可为对象：

```json
{"character_id":1002,"entry_id":"my-finished-copy","outfit_id":"100201",
 "locked":true,"status":"owned","red_factors":{"dirt":3}}
```

状态为 `owned`、`borrowable`、`planned`、`unknown`。支持的动作：

- `parents.profiles`：三份资料摘要、当前完整角色列表及可用概率模型。
- `parents.evaluate`：`tree.nodes` 固定七项（key／label／name／character_id／status），`tree.edges` 为祖→亲→目标；另有基础相性分项、可行性、实际共同胜场、改泥等确定性缺星。未知不当零；不合法组合仍可参考查看并显示问题。
- `parents.suggest`：自动补缺少的父母／祖辈，锁定节点保留；默认已填节点也保留。显式 `replace_unlocked=true` 才可替换未锁节点。支持 `candidate_character_ids`、`statuses`、`available_only`、`mode="characters"`、`limit`、`max_pairs`。达到组合预算会返回 `truncated=true`，不冒称全局最优。日服 135 人参考池实测约 2 秒、17,822 个有序亲代组合；这里不是实机性能保证。
- `inventory.demo`：读出演示 JSON，不写入。`inventory.import` 携带 `demo=true` 才显式保存演示库。
- `inventory.import/save`：`inventory` 或 `data` 对象（也可传 `entries`）；`inventory.list` 列本候选用户库，或带 `library_id` 读选中库；`inventory.export` 包含原始导入字段。
- `aptitude.gap`：`baseline_rank/target_rank/known_stars/unknown_nodes/factor`，或 `nodes`＋`aptitude_goal`；最多开局提升四级、最高 A，S 不显示为若干星即可保证。
- `races.plan`：实际胜场和计划胜场分开、共同 G1 候选、每半月最多一场的参考排程及冲突；不会写实际胜场或给未经校准的精确相性加成。

## 导入兼容性

接受：`uma.parent_library.v1` 的 `entries[].self/parents/factors/race_records`；Legacy 风格的 `veterans[].chara_id/card_id/factors/g1_wins/when_used_as_parent`；或通用 `entries/seeds` 列表。原条目额外字段存入 `imported`，普通 list 不回显整份原对象，export 显式返回。没有归属声明时默认 `unknown`；调用方可让用户明确选择 `import_default_status="owned"` 等，不能把 `self.status="complete"` 误当已拥有。

没有提供目标库 ID 的导入会生成新候选库，不会按导入文件自带 ID 覆盖其他库。`save` 可带 `expected_revision`，冲突返回当前 revision。单次马库路径只允许本候选内安全 ID；不读取导入对象中的本地路径、图片路径或凭据字段。

成品真实祖辈绑定到该成品，不会被自动补祖改写；要设计新祖辈，亲代需显式设为 `planned`。跨服使用另一服马库时仍可参考人物关系，但转为 `cross_server_reference`，不冒称本服 owned、实际胜场或已确认红星。

返回节点的 `entry_character_id` 是成品角色绑定。再次提交完整节点时，若手动更改／清空 `character_id` 却仍带原 `entry_id`，会降为 `status="unknown"` 的角色参考，清除原成品的归属、衣装、属性、因子、胜场及自动带入祖辈，并在 `feasibility.issues` 返回 `entry_character_mismatch`；相性参考计算仍继续。仅显式独立选择的祖辈保留，旧 `ancestry_bound_to_entry` 祖辈会解除并清空。即使不再载入马库，完整返回节点自带的绑定也会用于识别这一类编辑。

若直接亲代已没有 `entry_id`，该侧仍标记为 `ancestry_bound_to_entry=true` 或 `source_kind="recorded_ancestor"` 的孤立旧祖辈也会清空，避免其观察因子／胜场串入新亲代。独立选择且未标记绑定的祖辈不受影响；直接亲代仍带原 `entry_id` 的无库完整节点恢复也保留原祖辈。

把已有成品改成 `planned` 表示另做一匹新种马：旧 `entry_id` 脱离并保留为 `recipe_from_entry_id` 参考，不继承旧实际因子／属性／胜场／祖辈；仅保留明确的 `red_target` 和 `planned_wins` 计划。仍选相同角色和原成品时资料正常保留，原候选马库文件不会因参考查询而被修改。

## 可选概率

默认 `probability=null`。显式 `probability_model="legacy-modern-reference-v1"` 才计算 1%／3%／5%、两次独立继承、单次升一级的参考分布，返回 `model_id/assumptions/live_server_calibrated=false`。`forecast_planned=true` 仅给计划因子达成条件下的假设结果；不会改变实际星数。`include_reference_g1=true` 才按现代每链接 +3 的假设加入共同 G1，主结果仍不宣称当前服精确加成。

所有结果均 `tap_or_purchase_authorized=false`；本模块只查询、规划和保存显式导入的数据。
