# MaaUma 正式启动入口候选

本目录只提供下一版可整合的本地候选，没有修改或发布 0.4.37 包，也没有启动真实面板或操作设备。

## 交付文件

将 `artifacts/` 下四个文件放在面板根目录：

- `MaaUma.exe`：面向用户的正式名称与 PE 头像。
- `MaaUma.Entry.dll`
- `MaaUma.Entry.deps.json`
- `MaaUma.Entry.runtimeconfig.json`

四个文件总计 484,154 字节，具体长度与 SHA 见 `checks/build-result.json`。`MaaUma.exe` 是 WinExe 原生 apphost；它内嵌 `AppHostRelativeDotNet=dotnet`、`AppHostDotNetSearch=AppRelative`，无需依赖全局 .NET 安装或调用命令窗口。

托管入口原样转发参数到同目录的 `MFAAvalonia.exe`，通过 ArgumentList 调用，不经过 shell；cwd 指向面板目录，DOTNET_ROOT / DOTNET_ROOT_X64 指向随包 dotnet，其余环境保持继承。启动器随后退出，面板按既有生命周期运行、关闭和更新。

旧 `MFAAvalonia.exe`、`MFAAvalonia.dll`、`libs/`、`dotnet/` 必须保留。不要把旧 exe 直接改名。新托管文件命名为 `MaaUma.Entry.*`，不与个人面板已有的 `MaaUma.Launcher.exe` 冲突。

## 为何使用 wrapper

0.4.37 的 `AppRuntime.CreateInstanceKey` 把 exe 的完整路径、exe 哈希、Core 哈希共同组成 mutex/pipe 键。若新 apphost 直接加载 `MFAAvalonia.dll`，新旧入口会生成不同身份，可能并行打开同一个面板目录。

wrapper 使新旧入口最终都运行原来的 `MFAAvalonia.exe`，从而保留原有单实例键和进程识别。当前安装/回退助手等待的是 GUI 的 parent PID；`scripts/maauma_service.py` 重启同目录旧 exe。这些调用无需为本候选改名或改全项目身份逻辑。

## 头像来源

真实窗口链为 `RootView.Icon / LogoContent → IconHelper → interface.json 的 icon`。当前 0.4.37 指向 `resource/umaa-hishi-miracle.png`，不是上游默认 `Assets/logo.ico`。

`build.py` 从当前基线读取该图，并直接编译同一 GUI 的 `UmaIconRendering.cs`，由其 `WindowIconBytes` 生成 16、20、24、32、40、48、64、128、256 九档图标。没有重绘、替换角色或只修改快捷方式。PE 中 RT_GROUP_ICON / RT_ICON 的所有帧均与生成 ICO 逐字节匹配。原图与渲染源的 SHA 记录在 build-result 中。

## 验证与边界

本机已运行 `build.py` 与 `verify.py`。后者只使用临时 fake GUI：

- 原生 x64 PE、GUI 子系统与九档内嵌图标资源。
- 中文及空格目录，空参数、引号、反斜杠和 shell 特殊字符原样传递。
- 实际 CreateProcess，child 的进程名/完整路径仍为原入口，cwd 正确。
- 外部 DOTNET_ROOT 故意设为不存在路径时，仍使用随包 dotnet；child 的 console window 为 0。
- wrapper 先退出，child 独立继续并正常结束；未停止或收养 GUI 进程。
- 缺主程序和无效 PE：返回失败并给出中文说明；此机无效 PE 系统码为 216。

`checks/verification-result.json` 为实际结果。负例以同一 Run 路径注入文本接收器捕获说明，没有弹出生产错误对话框；对话框视觉样式未验收。若 `dotnet/` 自身丢失，原生 apphost 会在托管入口前给出 .NET 标准运行时错误；应引导用户重新完整解压，而不是只移动一个 exe。

## 下一版整合点

协调者可把这四个新增文件同时纳入基础包完整清单、增量文件清单和公开构建输入，并将用户打开入口指向 `MaaUma.exe`。Pacer 继续共享基础程序，无需另建启动器。旧入口、安装助手、来源/许可证说明均保留。已发布 0.4.37 的 ZIP、SHA、dist 和当前个人 app 都不在本候选修改范围内。

## 本地重建

在当前工作树，使用项目 Python 运行本目录 `build.py`，随后运行 `verify.py`。构建只使用现有 SDK、缓存的 Avalonia.Headless 和当前基线已有渲染库；NuGet.Config 清空网络源，不安装新依赖。构建/测试输出与 temp 均留在本目录；测试 dotnet 使用指向既有基线 runtime 的本地 junction，不复制整套运行时。
