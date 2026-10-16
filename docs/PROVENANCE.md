# 来源与修改说明

## 代码基准

本仓库以 MaaUma 0.4.5 普通版的发布源码为基准，进行独立源码布局和公开范围整理。版本、源快照摘要及来源指向见 [SOURCE-ORIGIN.json](../SOURCE-ORIGIN.json)。该记录不表示复制了原工作区或其历史。

GUI 上游为 [MaaXYZ/MFAAvalonia](https://github.com/MaaXYZ/MFAAvalonia)。保留的 SukiUI 源来自 [kikipoulet/SukiUI](https://github.com/kikipoulet/SukiUI)，Markdown 与 ColorDocument/ColorTextBlock 项目源来自 [whistyun/Markdown.Avalonia](https://github.com/whistyun/Markdown.Avalonia)。各上游的版权及许可见 [NOTICE.md](../NOTICE.md) 和 [licenses/](../licenses/)。

## 2026-10-16 源码整理与改动

- 整理普通版 GUI、普通 Python 后端和应用自定义配置，建立仓库内相对引用与独立构建入口。
- 补充桌面启动项目、Identity/PublicCheck 客户端工具源码及其构建入口。
- 保留普通版版本更新、测试版激活、种马库与规划等客户端逻辑。
- 补上游 Desktop 的启动程序和应用清单，调整为仓库内相对项目引用。
- 移除 Inter/Roboto/AlibabaPuHuiTi 字体绑定和旧 Suki HintPath，使用系统字体；移除 icons8 图像引用，替换为代码 PathIcon。
- Markdown 缺图回退改为代码生成的透明占位；窗口标题图标由自有 M 几何生成，SVG保留作标志来源说明。
- Identity 工具固定 Newtonsoft.Json 的 PackageReference，不依赖旧 DLL 路径。
- 排除没有纳入本次代码授权的历史媒体输入。
- 保留已完成的侧栏页脚集合隔离、图标渲染及普通客户端 UI 调整。
- 补齐 GPLv3、SukiUI/Markdown MIT 等许可副本与署名说明。

上述代码改动属于本次 GPLv3 代码发布范围；第三方原有权利仍按其条款保留。私有引擎、服务端、游戏资源、模型、数据库与个人数据没有因此进入普通源码仓。

## 验证与适用范围

源码布局、构建、离线检查和运行输入是分别验证的事项。能够编译不表示已提供全部运行资源，不表示新增真实设备/游戏验收，也不承诺生成历史安装包的逐字节副本。具体构建命令见 [BUILD.md](BUILD.md)。
