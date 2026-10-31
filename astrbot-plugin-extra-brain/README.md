# astrbot_plugin_extra_brain

把 [extra_brain](../docs/README.md) 长期记忆库挂载到 [AstrBot](https://github.com/AstrBotDevs/AstrBot)：每轮注入跨会话记忆、按轮巩固对话。这是一个**薄适配器**——只翻译挂载契约（见 `../docs/MOUNT.md`），记忆逻辑全在 extra_brain 库里。

## 功能

- **每轮注入**：LLM 请求前把 Core Memory + 检索命中的语义/情节记忆追加进 system_prompt（`@filter.on_llm_request()`）
- **按轮巩固**：累计 N 轮（默认 8）自动后台巩固；或随时 `/memory consolidate` 手动触发（`@filter.on_llm_response()` + 阈值缓冲，`asyncio.to_thread` 不阻塞事件循环）
- **peer 隔离**：私聊按人、群聊按群、WebChatUI 按登录用户（跨对话共享）——一个 peer 一个 SQLite 库
- **降级安全**：注入失败照常对话；巩固失败消息回缓冲下轮重试；无 `DEEPSEEK_API_KEY` 时只读
- **双后端**：`local`（进程内直连，零部署）/ `http`（调用 `extra_brain.server` sidecar）

## 安装

1. 把本目录放进 AstrBot 的 `data/plugins/astrbot_plugin_extra_brain/`（junction 或拷贝）
2. 确保 AstrBot 的 Python 环境能 import `extra_brain`：
   ```powershell
   <astrbot venv python> -m pip install -e D:\projects\small_fat_fish --no-deps
   ```
   开发期用 editable 安装指向仓库 `src/`，改记忆库代码重启 bot 即生效。
3. （巩固功能）启动 AstrBot 前设置环境变量 `DEEPSEEK_API_KEY`；不设则只读
4. WebUI 插件页确认已启用；指令 `/memory status` 应显示 peer_id 与后端信息

## 配置（WebUI 插件配置面板）

| 键 | 默认 | 说明 |
|---|---|---|
| `backend` | `local` | `local` 进程内直连 / `http` 走 sidecar |
| `extra_brain_src_path` | 空 | local 后端的 extra_brain 源码 src 目录（留空用 pip 安装版） |
| `data_dir` | `data/extra_brain` | 记忆库数据目录，每 peer 一个 .db |
| `base_url` | `http://127.0.0.1:8756` | http 后端的 sidecar 地址 |
| `peer_scope` | `auto` | `auto` 智能剥离（webchat 按用户）/ `full_umo` 严格按会话源 |
| `inject_enabled` | true | 关闭则只积累不注入 |
| `consolidate_every` | 8 | 自动巩固轮数阈值 |
| `context_turns` | 5 | 检索携带的近期对话轮数 |
| `token_limit` | 0 | 注入 token 预算（0 = 库默认 1800） |

## 指令

```
/memory status                # peer、后端、待巩固轮数、阈值
/memory consolidate           # 立即巩固缓冲中的对话
/memory core                  # 查看 Core Memory（JSON）
/memory list [semantic|episodic|style]   # 列出记忆（前 20 条）
/memory search <查询>         # 检索记忆（仅 local 后端）
```

## 验证（跨会话暗号法）

**WebChatUI**：
1. 会话 A：`记住：我的记忆测试暗号是「蓝鲸-7749」。`
2. `/memory consolidate`，等 1 分钟（含 LLM 抽取）
3. **新建**会话 B：`我上次告诉你的记忆测试暗号是什么？` → 答出「蓝鲸-7749」即注入+巩固+peer 三链路全通

**QQ 私聊**（NapCat + OneBot v11 反向 WS，`ws://127.0.0.1:6199/ws`）：
1. 另一 QQ 号私聊机器人：`记住：我的QQ记忆测试暗号是「海豚-3311」。`
2. `/memory consolidate`，等 1 分钟
3. `/reset`（清空对话上下文）
4. `我上次告诉你的记忆测试暗号是什么？` → 答出「海豚-3311」✅（已验证 2026-11-01）

QQ 侧 peer 天然按 QQ 号隔离（`aiocqhttp:FriendMessage:<QQ号>`），群聊按群号；无需任何配置。

## 已验证环境

- AstrBot 4.29.4（Python 3.12，conda env `astrbot`）
- extra-brain 0.1.0（editable 安装指向仓库 src/）
- WebChatUI + QQ（NapCat Shell 4.18.41 / QQ NT 9.9.33-52230，机器人号 1178469499），DeepSeek `deepseek-flash` 对话 + 抽取
- 2026-10-31 WebChatUI 跨会话暗号验证通过；2026-11-01 QQ 私聊跨上下文暗号验证通过（详见 `../docs/CHANGELOG.md`）
