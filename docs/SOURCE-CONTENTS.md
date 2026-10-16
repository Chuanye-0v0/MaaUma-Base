# 源码交付范围

本次公开范围以 0.4.5 普通版为基准；来源记录在 [SOURCE-ORIGIN.json](../SOURCE-ORIGIN.json)。以下内容分别检查，不能仅凭 GUI 工程存在就宣称每个随包可执行件都能重建。

| 部分 | 仓库位置与说明 |
| --- | --- |
| 普通 GUI | `gui/MFAAvalonia/`，以及其仓库内项目引用 |
| 兄弟项目 | `gui/SukiUI/`、Markdown 与 ColorDocument/ColorTextBlock 项目、`gui/LazyStaticGenerator/`；保留独立上游通知 |
| 普通 Python 后端与代理 | `backend/`、`agent/`；只包括本次公开的普通客户端代码和应用自定义配置 |
| 桌面启动与客户端工具 | `gui/`、`tools/` 中对应源码；构建入口包括 Desktop、Identity、PublicCheck 目标 |
| 构建入口 | `scripts/build.py`；依赖版本见各项目文件和 `requirements.txt` |
| 应用配置与资源接口 | 根应用配置、`config/`、`resource/` 中公开的应用定义，及 `resources/required-inputs.json` |
| 许可与署名 | 根 `LICENSE`、`NOTICE.md`、`licenses/` |

运行资源、模型、游戏数据、预编译运行环境、私有引擎、服务端及个人状态不是这份 Git 源码的组成部分。缺少运行输入时，某项功能不能直接使用，并不表示需要把未知许可的数据自动补入代码仓。

验证时先执行源码布局检查，再按 [BUILD.md](BUILD.md) 构建三个客户端目标。组装运行目录是另一个步骤，须显式提供外部运行环境。验证记录应区分构建成功、离线检查通过及真实设备验收；本仓库不对最后一项作自动承诺。
