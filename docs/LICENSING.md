# 许可证范围

## 本仓库代码

MaaUma Base 的 MFAAvalonia 派生 GUI 以及本次公开的 MaaUma 原创新增/修改，以 GNU General Public License 第 3 版提供。完整条款在根目录 [LICENSE](../LICENSE)。这是代码授权声明，不覆盖没有纳入该授权的第三方内容。

原有上游版权、许可证和源文件中的署名继续保留。对这些代码的修改、再分发与对应源码提供，应遵守相应许可证。未来新增的第三方代码也需保留其原始授权和通知，不能只加一个根许可证便略过来源。

## 已复制的上游源码

| 范围 | 许可与通知 |
| --- | --- |
| MFAAvalonia、其 LazyStaticGenerator 和 MaaUma 派生修改 | GPLv3；[上游副本](../licenses/MFAAvalonia-GPL-3.0.txt) |
| SukiUI | MIT；[原版权和许可全文](../licenses/SukiUI-MIT.txt) |
| Markdown.Avalonia、Tight、Html/Math/Svg/SyntaxHigh、ColorDocument/ColorTextBlock 相关源 | 上游 MIT；[许可全文](../licenses/Markdown.Avalonia-MIT.txt)，并保留各项目元数据中的作者/版权，见 [NOTICE](../NOTICE.md) |

MaaFramework/绑定依赖采用 LGPLv3，参考副本为 [MaaFramework-LGPL-3.0.md](../licenses/MaaFramework-LGPL-3.0.md)。动态链接、再链接、通知和其他具体条件以完整原文为准。NuGet/Python 依赖仍分别受自己的条款约束；源码仓的 GPL 声明不把依赖变成 GPL。

## 未随 Git 分发的输入

本仓库不向游戏图像、数据库/派生数据、OCR 模型、预编译运行库或其他独立资源追加许可。按代码许可可公开源代码，不等于所有运行输入都可以用同一许可再发布。所需输入和排除范围见 [RESOURCES.md](RESOURCES.md)。

已发布普通应用的预览分发决定，也不替代第三方版权许可。若将源码与外部输入组装为另一份二进制包，应另外保留并满足其中各项依赖与资源的适用条款。

## 来源记录

[licenses/ORIGINS.json](../licenses/ORIGINS.json) 记录从官方项目获取的许可文件 URL、Git blob 标识、SHA-256 与获取时间；许可副本保持原文。当前源码基准与调整说明在 [PROVENANCE.md](PROVENANCE.md)。
