# MaaUma Base

MaaUma 普通版客户端的开源代码，面向 Windows，包含桌面界面、Python 后端与构建工具。

现有功能包括：

- 日活任务管理。
- 开种与收菜。
- 种马库与种马规划。
- 技能与因子方案。
- 公共系统设置与消息推送。
- 种马因子目标命中通知。
- 更新下载进度、传输速度与等待提示。

各功能所需的识别图、数据库和模型等运行输入独立提供，详见[运行资源说明](docs/RESOURCES.md)。

## 构建

建议使用 Windows x64、Python 3.11 和 .NET 10 SDK。从仓库根目录运行：

```text
python scripts/build.py check
python scripts/build.py build --dotnet <dotnet可执行文件> --work .build
```

`--work` 使用尚不存在的新目录。构建会还原声明的 NuGet 依赖，生成桌面客户端、本机凭据辅助工具和离线检查工具。完整的构建、离线缓存与运行目录组装步骤见[构建文档](docs/BUILD.md)。

## 问题反馈

请通过 [GitHub Issues](https://github.com/Chuanye-0v0/MaaUma-Base/issues/new/choose)反馈问题，填写版本、复现步骤和实际表现；无法稳定复现时可注明“偶发/步骤不明”。提交需要登录 GitHub，内容及附件公开可见，请勿包含账号、密码、密钥及个人信息。

## 运行资源与许可证

本仓库代码以 **GNU GPL 第 3 版**提供，保留 MFAAvalonia 等上游版权和第三方独立许可。完整条款见 [LICENSE](LICENSE)，署名与范围见 [NOTICE.md](NOTICE.md)。

游戏识别图、头像、技能与因子数据库、OCR 模型及预编译运行环境不随本仓库分发，也不因代码的 GPL 声明而获得额外授权。组装运行目录时，需自行提供版本匹配且具有适用使用许可的输入，保留其来源和许可说明。

构建与离线检查不等于真实设备或游戏验收，也不承诺与历史安装包逐字节一致。

- [运行资源与许可边界](docs/RESOURCES.md)
- [许可证与第三方说明](docs/LICENSING.md)
- [来源与修改记录](docs/PROVENANCE.md)
- [源码交付范围](docs/SOURCE-CONTENTS.md)
