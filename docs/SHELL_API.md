# Chat Shell API 契约（v0.13.3）

服务：`python -m extra_brain.server --data-dir extra-brain`（默认 `127.0.0.1:8756`）。

- **鉴权**：除 `GET /v1/health` 外，所有 `/v1/*` 端点需要 `Authorization: Bearer <token>`
  或 `X-Shell-Token: <token>` 请求头；token 来源优先级：`SHELL_TOKEN` 环境变量 >
  `user_config.json` 的 `shell.shell_token` > 启动时随机生成。**启动日志始终打印当前 token 值**
  （`[shell on (peer …, token …)]`，仅输出到控制台），网页首次打开把它粘贴进 Token 弹窗即可（存 localStorage，只需一次）。
  token 错误/缺失 → `401 {"error": "invalid or missing shell token"}`。
- **绑定**：默认 `127.0.0.1`（配置强制），请勿对公网开放。
- **默认 peer**：`local-main`（可用 `SHELL_PEER_ID` / `shell.shell_peer_id` 改）；请求体可传
  `peer_id` 覆盖。每个 peer 一个 SQLite，文件名为编码后的 `<data-dir>/<safe>-<md5前8>.db`
  （与 AstrBot 插件同一编码，`qq:FriendMessage:1975824324` → `qq_FriendMessage_1975824324-e46c8efb.db`）。
  **与 QQ 记忆互通**：把 `shell.shell_peer_id` 设为 QQ 私聊 peer（如 `qq:FriendMessage:1975824324`）即可共用同一记忆库；
  全局伴侣库默认同为 `<data-dir>/_global_companion.db`，人设跨端一致。
- **Web UI**：`GET /` 或 `/shell/` 返回聊天页（token 存 localStorage）。主界面只有聊天 + 发送，记忆**自动巩固**；
  顶栏「高级」抽屉（默认收起）保留注入预览与「立即写入记忆」等排查能力。

---

## GET /v1/health（免鉴权）

```json
{
  "ok": true,
  "version": "0.13.3",
  "peer_id": "local-main",
  "read_write": true,
  "data_dir": "D:\\projects\\small_fat_fish\\extra-brain",
  "auto_consolidate": {
    "enabled": true,
    "every_n_user_messages": 8,
    "on_new_session": true,
    "on_page_hide": true,
    "min_messages": 2
  }
}
```

`read_write=false` 表示未配置 API Key 或服务以只读模式运行（chat/consolidate 将 503），读取端点不受影响。

`auto_consolidate` 是**下发策略**：巩固的**触发在 shell 前端**（攒满 N 条 / 开新会话 / 页面隐藏时，把未巩固切片 POST 到
`/v1/consolidate`），服务端不会自己定时巩固，因此不会出现双重写入。策略来源优先级：`SHELL_AUTO_CONSOLIDATE_*`
环境变量 > `user_config.json` 的 `shell.auto_consolidate_*` > 默认值。

---

## GET /v1/inject_preview?q=<用户最后一句>&peer_id=local-main

也支持 POST：`{"peer_id": "...", "query": "...", "recent_context": ["user: ...", ...]}`

```json
{
  "peer_id": "local-main",
  "text": "【契约】…（build_injection 原文，含 Core/记忆/自我状态/安全规范）",
  "token_estimate": 633,
  "meta": { "memory_ids": ["sem_20261106_0001"] }
}
```

**只读**：走只读 brain 实例，不产生激活/加固/衰减写入。

---

## POST /v1/consolidate

```json
{
  "peer_id": "local-main",
  "session_id": "shell-20261106-001",
  "messages": [
    {"role": "user", "content": "我叫小舟，请记住我下周三相亲面试会紧张"},
    {"role": "assistant", "content": "记住了…"}
  ],
  "now": null
}
```

响应与内核巩固报告对齐（JSON 可序列化）：

```json
{
  "semantic_written": 2,
  "episodic_written": 1,
  "conflicts": [],
  "core_pruned": [],
  "peer_id": "local-main"
}
```

无 Key / 只读模式 → `503 {"error": "read-only mode: …"}`。

---

## POST /v1/chat（核心）

```json
{
  "peer_id": "local-main",
  "session_id": "shell-...",
  "message": "用户本句",
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "auto_inject": true,
  "model": null
}
```

服务端逻辑：`auto_inject` 时用 `message` + 最近 5 轮 history 调 `build_injection`（只读）→
system = 人设正文（`persona_file`，v0.12.1 同源）+ 注入文本 + 安全规范（`safety_rules`）→
调现有 LLM 客户端（与 consolidate 同一套 api 配置：`user_config.api` → `DEEPSEEK_API_KEY`）。

```json
{
  "reply": "助手回复",
  "session_id": "shell-...",
  "injected": true,
  "inject_token_estimate": 500,
  "usage": { "total_tokens": 1024 },
  "peer_id": "local-main"
}
```

- LLM 不可达 → `502 {"error": "LLM call failed: …"}`；无客户端 → `503`。
- **本版默认不在服务端自动巩固**（由 UI「记住以上对话」按钮或客户端策略调 `/v1/consolidate`）。
  可选 `shell.auto_consolidate_every_n`（默认 `0` = 关）：每 N 轮自动巩固，报告在响应的
  `auto_consolidate` 字段（失败则 `auto_consolidate_error`，不阻塞回复）。

---

## 调试只读端点

| 端点 | 说明 |
|------|------|
| `GET /v1/core?peer_id=…` | Core Memory JSON（默认 peer 可省略参数） |
| `GET /v1/stats?peer_id=…` | 存储分层统计 |
| `POST /v1/set_owner` | 写主人身份卡到全局伴侣层 Core：`{"entries": ["主人QQ:1975824324|称呼:JCzero|关系:父女"]}`（幂等） |
| `GET /v1/owner` | 读当前主人身份卡 |

## 旧 sidecar 端点（不变，AstrBot http 后端兼容）

`/health` `/inject` `/consolidate` `/forget` `/core` `/list` `/stats` `/proactive` 等维持原样，
不受 shell 鉴权影响；`--no-shell` 可整体关闭 `/v1/*` 与 UI。

## 回归

`python scripts/shell_regression.py`：health → chat×3 → consolidate → 新 session 召回 →
关键词断言，退出码 0/1（无 `DEEPSEEK_API_KEY` 时 SKIP）。
