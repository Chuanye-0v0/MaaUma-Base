# MaaUma Base

MaaUma 基础版客户端源码，基于 0.4.5 普通版整理。包含普通日活、开种与收菜、技能和因子方案、种马库及种马规划的客户端与后端代码。

本仓库代码以 **GNU GPL 第 3 版**提供，保留 MFAAvalonia 等上游版权和第三方独立许可。完整条款见 [LICENSE](LICENSE)，署名与范围见 [NOTICE.md](NOTICE.md)。

## 源码与运行资源

仓库提供 GUI、普通 Python 后端、客户端工具和构建入口。游戏识别图、头像、技能/因子数据库、OCR 模型、预编译运行环境均作为独立输入，不随 Git 仓库分发，也不因本仓库 GPL 声明而获得额外授权。所需文件位置见 [resources/required-inputs.json](resources/required-inputs.json) 和[运行资源说明](docs/RESOURCES.md)。

公开客户端保留版本更新和“测试版激活”入口。个人授权只影响相应服务及扩展的访问权限；本仓库不包含个人码、授权回执、账号记录、私有育成/刷切引擎或服务端实现。

## 构建

建议使用 Windows x64、Python 3.11 和 .NET 10 SDK。从仓库根目录运行：

```text
python scripts/build.py check
python scripts/build.py build --dotnet <dotnet可执行文件> --work .build
```

`--work` 使用尚不存在的新目录。构建会还原声明的 NuGet 依赖，并生成 `.build/build-result.json` 和 `.build/publish/{gui,identity,check}/`。已有完整 NuGet 缓存时可加 `--offline`。若要组装可运行目录，需另外提供具有合法使用来源、版本匹配的完整普通运行环境：

```text
python scripts/build.py assemble --work .build --runtime-base <外部普通运行环境目录> --output <新的应用目录>
```

完整步骤和输入边界见[构建文档](docs/BUILD.md)。外部运行环境仅在组装时读取，不应加入本仓库。

## 验证范围

这是从 0.4.5 普通客户端整理的独立源码布局；使用系统字体和自有几何标志。构建与离线检查不等于真实设备或游戏验收，也不承诺在不同 SDK/NuGet 环境下生成与已发布安装包逐字节相同的二进制。

- [构建与离线检查](docs/BUILD.md)
- [代码、资源与授权边界](docs/RESOURCES.md)
- [许可证与第三方说明](docs/LICENSING.md)
- [来源与修改记录](docs/PROVENANCE.md)
- [源码交付范围](docs/SOURCE-CONTENTS.md)

## 问题反馈

基础版问题可通过 [GitHub 问题表单](https://github.com/Chuanye-0v0/MaaUma-Base/issues/new/choose)反馈，无需激活码；提交时需要登录 GitHub。Issue 及其附件默认公开，请填写版本和复现步骤，并遮挡私密信息。

涉及测试功能、账号或授权资料，请使用[私密反馈渠道](https://maauma-private.2234198378.workers.dev/feedback)，不要提交到公开 Issue。
