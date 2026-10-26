# 自包含知识与 SP 候选数据

运行期仅从本目录读取静态数据。`stage.py` 的原始路径是构建期来源，不要求使用者拥有 `C:/Users/Administrator/.codex/worktrees/077a`，启动查询也不会联网。

## 实际覆盖

- 2,643 个独立技能 ID：合并 .38 的1,284查询条目、1,260历史丰富条目、JP外部名字表及三服Wiki数据。包含旧技能、继承版和名字覆盖项，不等于2,643项在某服当天均已实装或均可模拟。
- 丰富成本／关系仍以1,260条凯旋门末期库为主要历史基础；JP补充费用来自固定外部项目metadata。CN/TW有独立Wiki名字与描述，不自动繁简转换。
- 165个人物条目、284衣装；每个名字保留来源与状态。原有user-confirmed/curated CN名优先，社区／Wiki名不冒称官方确认。未知衣装译名保持缺失。
- 489张支援卡名字，其中354张有历史技能机会数据，含1,537事件、259事件链。卡组可以少于六张查询；满六张也不代表提示和事件必得。

三服Wiki机械表数量分别为CN1,289、TW1,061、JP1,271；这些表的修订时间不一致。JP外部模拟引擎参考另有其自己的技能覆盖。返回值标明选中服的Wiki机械、JP外部参考、历史CN参考或缺失，不把JP效果悄悄当作CN效果。

`manifest.json` 保存原始来源SHA、Wiki修订与本目录文件清单；`raw/` 保留原始数据模块。解析器只解析生成的数据表，不执行下载的Lua。

## 入口

```python
from hub.knowledge import dispatch
dispatch({'action':'skill.search','query':'弧线大师'})
dispatch({'action':'entity.names','query':'樱花劳蕾尔','kind':'character'})
dispatch({'action':'skill.search','query':'黄金巨匠固有','server':'jp'})
dispatch({'action':'support.deck','card_ids':['10001','10002']})

from hub.sp import dispatch as sp_quote
sp_quote({'action':'sp.quote','skills':[{'skill_id':'200331','hint_level':3}],
          'learned':['200332'],'cutting_edge':True})
```

知识查询返回`items`、`total`和来源；技能的`id/db_id`均是独立数值字符串，原生、低星固有与继承版本不按同名合并。`skill.detail`另返回`skill`对象；`entity.names`可返回人物和衣装记录。空查询可列目录，不要求先填完赛道卡片。

`sp.quote`返回每项`cost:{low,high}`与总计`total:{low,high,exact,status,known_subtotal}`。`exact`只表示算术上下界重合，不证明该服当前价格已经实机核对。没有价格或下位链时总价保留未知，不当作零。

提示0～5分别减0/10/20/30/35/40%；切者额外10%，逐级相加。每级有自己的提示，未指定下位提示按0；已学下位不重复收费，同时选下位与上位不重复算费用链。提供`displayed_final_cost`时不再打折。非整数折扣保留向下／向上取整区间。使用跨服参考价会单独标记并警告；卡组只提供技能机会，不自动认为已有提示。

黄金巨匠的原生固有映射来自固定kachi项目`HorseDef.tsx`实际角色选择公式，现可返回101151。当前抓取机械表没有其901151继承行的显式holder关系，所以角色固有查询报告该继承链接缺口；直接按901151查询和报价仍保留独立ID，不能仅凭同名把关系当作已验证。

## 来源与许可范围

- 用户项目已发布0.4.38静态目录与077a历史知识库，来源说明原样保留。
- [BWIKI技能数据](https://wiki.biligame.com/umamusume/)：按manifest中的具体模块和修订保存，网页／数据许可与代码许可分开记录。
- [kachi-dev/uma-tools](https://github.com/kachi-dev/uma-tools/tree/4401e25987d9c78707f2d3f8a9dcd02c1c56529c)：JP技能名字、机械参考、费用metadata和人物衣装；项目GPL-3.0-or-later，保留相应许可证。数据和图片再分发范围未在本候选中独立确认，不把代码开源声明当作素材授权。

此目录只供当前本地候选使用，未覆盖正式程序，没有游戏、真实账号或QQ操作。
