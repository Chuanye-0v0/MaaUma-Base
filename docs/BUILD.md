# 构建与组装

以下命令均从仓库根目录执行，不依赖原开发机的工作树、历史包目录或绝对路径。

## 工具

- Windows x64；本仓库当前面向该目标整理。
- Python 3.11。
- .NET 10 SDK。
- 构建时可访问项目声明的 NuGet 源，或已准备兼容的本地缓存。

`requirements.txt` 固定了普通 Python 运行环境的直接依赖版本。构建 GUI 不会因此自动取得游戏识别图、数据库或运行模型；这些文件另见[资源说明](RESOURCES.md)。

## 检查源码

```text
python scripts/build.py check
```

该入口检查源码布局和公开范围。它不表示已完成真实游戏任务，也不会授予设备操作权限。

## 构建客户端

```text
python scripts/build.py build --dotnet <dotnet可执行文件> --work .build
```

`--work` 必须指向尚不存在的新目录。构建工具把 `gui/`、`tools/` 源码复制到该目录，执行还原和发布，不把 `obj/` 从开发环境复制到源码中。成功后生成：

- `.build/build-result.json`：本次构建结果。
- `.build/publish/gui/`：普通桌面 GUI。
- `.build/publish/identity/`：Identity 辅助工具。
- `.build/publish/check/`：PublicCheck。

再次构建时请使用另一个新的工作目录；组装时则指定刚构建成功的同一 `--work`。源码仓本身不应保存生成的 `bin/`、`obj/` 或临时工作目录。

已有完整 NuGet 缓存时可显式使用离线还原：

```text
python scripts/build.py build --dotnet <dotnet可执行文件> --work .build-offline --offline
```

离线模式不会从网络补齐缺失依赖；缓存不完整时构建会失败。

Identity 是客户端本机身份/凭据保护辅助程序的源码，不包含用户的私钥、授权数据或服务端密钥。PublicCheck 是客户端离线检查工具，不替代真实设备验收。

## 组装运行目录

仅编译的 GUI 不含完整运行资源。若另行取得版本匹配、来源合适的完整普通运行环境，可组装到一个新目录：

```text
python scripts/build.py assemble --work .build --runtime-base <外部普通运行环境目录> --output <新的应用目录>
```

`--runtime-base` 是显式外部输入，应使用同版 0.4.5 通用普通运行环境的新解压目录，避免使用带个人状态的现用程序目录；构建工具不会替输入补齐许可。不要将整个外部运行环境复制进 Git。组装产生的新目录也应保持在仓库之外或被忽略的位置，避免提交个人配置和生成物。

## 后端环境

需要单独安装 Python 依赖时，可在自己的隔离环境中使用：

```text
python -m pip install -r requirements.txt
```

还需按 `resources/required-inputs.json` 准备相应运行输入。该清单只定义相对路径与接口位置，不分发数据本身，也不表示取得了这些输入的再分发许可。

不同 SDK 补丁、NuGet 版本解析或系统环境可能产生不同二进制。本仓库不承诺与历史发行 ZIP 字节重现；发布状态与真实设备验收应分别记录。
