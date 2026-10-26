# 种马规划与资料工具箱来源

既有基础相性事实参考赛马娘 BWIKI《相性计算器计算方法》固定修订 140988，作者和编辑历史见：
https://wiki.biligame.com/umamusume/index.php?title=%E7%9B%B8%E6%80%A7%E8%AE%A1%E7%AE%97%E5%99%A8%E8%AE%A1%E7%AE%97%E6%96%B9%E6%B3%95&oldid=140988

既有资料的原始来源、快照和范围保留在 data/parent_library 相关 JSON。历史技能和支援卡资料不能当作当前账号持有或技能必得的证明。角色正式国服名称和社区别名分开，卡片与技能按稳定编号区分，保留固有/继承、金白及不同版本边界。

0.4.40 新增的资料工具箱随包位于 knowledge-tools。查询、SP、相性、系谱和本地文字路由的源码分别位于 hub、web 和 adapters。技能、角色、支援卡和分服关系数据的具体来源、修订与许可说明见 data/knowledge/manifest.json、该目录 README.md 和 data/inheritance/README.md。数据目录合并多个历史/社区快照，不表示三服当日实装范围相同。

赛道参考引擎使用 kachi-dev/uma-tools@4401e25987d9c78707f2d3f8a9dcd02c1c56529c 的匹配源码，按 GPL-3.0-or-later 提供。对应源码随正式包置于 knowledge-tools/engine/vendor/kachi-uma-tools，本地 adapter.ts、build.mjs、入口、锁文件和 source/build-manifest.json 同时保留。完整许可证在 engine/LICENSE，变更说明在 engine/NOTICE.md；Prando/esbuild 的 MIT 许可证在 engine/licenses。

国服和繁中服模拟目前明确使用参考机制，尚未逐服校准；SP 为费用报价，不宣称全局最优选技或真实比赛胜率。程序代码许可证不替代游戏图像和资料的权利。本包保留第三方通知，不加入用户截图、真实账号库、凭据或开发预览保存状态。产品自带的示例马库明确标为 synthetic_demo，只有用户主动载入时使用。
