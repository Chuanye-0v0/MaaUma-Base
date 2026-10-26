# 聊天接入

本目录是薄适配器，数据和计算位于同一 `hub`，不再维护另一套费用／相性公式。

## 离线使用

在候选目录中导入：

```python
from adapters.qq import handle_message
reply = handle_message({
    "text": "/马娘 日服 菊花赏 黄金巨匠固有收益是多少",
    "user_id": "test-user", "group_id": "test-group",
    "render_image": True,
})
```

`reply['text']` 为文字，`reply['tool_result']` 为完整同源结果，显式 `render_image=True` 时 `reply['image']['path']` 指向生成的PNG。`sent=False` 表示这个函数没有调用任何QQ发送接口。生成卡片只读取本地字体，不下载外部头像。

QQ公开查询不自动附带本机马库；纯适配器会剔除传入的库路径／库选择项。导入私人马库应先做用户专属授权和映射，不把本机所有账号的资料开放给群聊。

## NoneBot 2

`nonebot_plugin.py` 提供 `/马娘`（别名 `/uma`）命令，调用 `MAAUMA_KNOWLEDGE_URL`，默认 `http://127.0.0.1:18765`。主框架和 OneBot／官方QQ适配器已有公开文档和 MIT 许可证；协议实现端及平台登录由用户配置。插件加载本身不登录QQ，也不创建群会话。

HTTP示例桥默认发文字。若与工具箱同机部署并需要PNG，可在自己的OneBot handler中使用上面的 `handle_message(...render_image=True)`，确认目标会话后将生成的PNG字节交给适配器的 `MessageSegment.image`；不要把本机文件路径直接发到群里。

本轮没有安装机器人框架、获取登录态或发送真实消息。离线消息与网页共用工具的验证在 `checks/e2e/`。
