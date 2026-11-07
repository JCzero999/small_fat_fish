# astrbot_plugin_extra_brain

把 [extra_brain](../docs/README.md) 长期记忆库挂载到 [AstrBot](https://github.com/AstrBotDevs/AstrBot)：每轮注入跨会话记忆、按轮巩固对话。这是一个**薄适配器**——只翻译挂载契约（见 `../docs/MOUNT.md`），记忆逻辑全在 extra_brain 库里。

## 功能

- **每轮注入**：LLM 请求前把 Core Memory + 检索命中的语义/情节记忆追加进 system_prompt（`@filter.on_llm_request()`）
- **按轮巩固**：累计 N 轮（默认 8）自动后台巩固；或随时 `/memory consolidate` 手动触发（`@filter.on_llm_response()` + 阈值缓冲，`asyncio.to_thread` 不阻塞事件循环）
- **peer 隔离**：私聊按人、群聊按群、WebChatUI 按登录用户（跨对话共享）——一个 peer 一个 SQLite 库
- **降级安全**：注入失败照常对话；巩固失败消息回缓冲下轮重试；无 `DEEPSEEK_API_KEY` 时只读
- **双后端**：`local`（进程内直连，零部署）/ `http`（调用 `extra_brain.server` sidecar）
- **v0.5.0 能力**（记忆库侧提供，随版本自动生效）：双时态 `as_of` 检索、四路召回、七因子保留度分档、只读挂载、静态加密（可选）、P1 墓碑遗忘。适配器的注入/巩固/指令链路**零改动**即享受全部升级——再次印证「记忆库是产品，宿主是挂载点」

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
| `data_dir` | `data/extra_brain` | 记忆库数据目录，每 peer 一个 .db。**本机已改为仓库 `D:/projects/small_fat_fish/extra-brain`（唯一真相源，见下节）** |
| `companion_db` | 空 | 全局伴侣库路径，本机为 `D:/projects/small_fat_fish/extra-brain/_global_companion.db`；需与 Shell 的 `--data-dir` 同名文件，否则人设 / 主人身份卡不互通 |
| `base_url` | `http://127.0.0.1:8756` | http 后端的 sidecar 地址 |
| `peer_scope` | `auto` | `auto` 智能剥离（webchat 按用户）/ `full_umo` 严格按会话源 |
| `inject_enabled` | true | 关闭则只积累不注入 |
| `consolidate_every` | 8 | 自动巩固轮数阈值 |
| `context_turns` | 5 | 检索携带的近期对话轮数 |
| `token_limit` | 0 | 注入 token 预算（0 = 库默认 1800） |
| `user_config_path` | `config/user_config.json` | 人设 / 安全规范 / API Key / 主人身份卡（`identity.owner_accounts`）来源 |

### 数据目录：仓库 `extra-brain/` 是唯一真相源

QQ、Web Shell、将来的抖音等都只是**挂载点**，`data_dir` 一律指向仓库 `D:\projects\small_fat_fish\extra-brain\`
（2026-11-07 起；原 `astrbot-runtime\data\extra_brain\` 保留作备份，不可再写入）。一个 peer 一个库，
文件名是 `_filename_for_peer(peer_id)`：`qq:FriendMessage:1975824324` → `qq_FriendMessage_1975824324-e46c8efb.db`
（与 Shell 的 `server.py` 同一编码，所以两端能打开同一个人）。

改 `data_dir` / `companion_db` 需重启 AstrBot；**不要重新初始化已有库**。

### v0.13.x 新增能力（适配器侧）

- **主人身份卡（v0.13.2）**：`user_config.json → identity.owner_accounts`（如 `"主人QQ:1975824324|称呼:JCzero|关系:父女"`），进程启动后首次调用时幂等写入**全局伴侣层 Core**——Core 始终完整注入，所以私聊 / 群聊 / 网页都认得出主人
- **群聊发言者标注（v0.13.2）**：群消息在检索 query 与巩固缓冲里统一加 `[发言者 <QQ号>|<昵称>]`，抽取器能把记忆绑定到具体的人，模型也知道这句话是谁说的；私聊保持原文
- **与 Shell 记忆互通（v0.13.1）**：插件 `data_dir` 与 Shell `--data-dir` 指向同一目录，且 Shell 的 `shell.shell_peer_id` 设为 `qq:FriendMessage:<QQ号>` 即可共用同一个库
- **注入预算**：真实库默认注入已接近 1728/1800，记忆继续增长时调大 `token_limit` 或精简热记忆
- **人设唯一源（v0.13.4）**：只认 `persona_file`（`persona/xiaotun_v1.md`），**前置**进 system prompt；面板里的旧人格排在后面，不再压过人设。不要再往面板里写第二份长人设
- **记忆使用约定（v0.13.4）**：`build_injection` 末尾统一带【使用约定】脚注（勿逐条汇报 / 勿用「我记着呢」/ 不确定就承认），开关 `user_config.tone.memory_usage_footer`（默认 true）

## 指令

```
/memory status                # peer、后端、待巩固轮数、阈值
/memory consolidate           # 立即巩固缓冲中的对话
/memory core                  # 查看 Core Memory（JSON）
/memory list [semantic|episodic|style]   # 列出记忆（前 20 条）
/memory search <查询>         # 检索记忆（多词查询自动合并；仅 local 后端）
/memory stats                 # 记忆库分层统计（保留度档位/归档比例/体积）
/memory forget <id>           # P1 墓碑化（清内容、留摘要与审计）
/memory forget <id> --hard --yes   # P0 物理删除（双确认，适配器不会自动批准）
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

QQ 侧 peer 天然按 QQ 号隔离（本机 AstrBot 的 UMO 前缀是 `qq`：`qq:FriendMessage:<QQ号>`，群聊 `qq:GroupMessage:<群号>`；旧版平台前缀可能是 `aiocqhttp`，以实际 `unified_msg_origin` 为准，文件名带 md5 后 8 位不影响稳定）；无需任何配置。

## 已验证环境

- AstrBot 4.29.4（Python 3.12，conda env `astrbot`）
- extra-brain **0.13.4**（editable 安装指向仓库 src/；适配器注入/巩固/指令链路自 v0.5 起零改动）
- WebChatUI + QQ（NapCat Shell 4.18.41 / QQ NT 9.9.33-52230，机器人号 1178469499），DeepSeek `deepseek-flash` 对话 + 抽取
- 2026-10-31 WebChatUI 跨会话暗号验证通过；2026-11-01 QQ 私聊跨上下文暗号验证通过（详见 `../docs/CHANGELOG.md`）
- 2026-11-01 **v0.5.0 升级回归通过**：备份 → 重启 → 旧记忆零丢失（QQ 15 条）→ 新暗号四步验证 → 七因子分档在真实数据生效
- 2026-11-07 **v0.13.1 互通验证**：插件与 Shell 指向同一 `extra-brain/` + 同一 QQ peer，Shell 只读注入 1709 tokens，QQ 侧记忆全召回
- 2026-11-07 **v0.13.2 群聊识别验证**：`owner_accounts` 写入全局 Core，群消息带 `[发言者 …]` 标注后模型能指出是谁在说话
- 2026-11-07 **v0.13.4 语气验证**：与 Shell 同读一份人设正文（前置）+ 注入脚注，五句语气回归无「我记着呢」类套话（Shell 侧已真实验证，QQ 侧按 `../scripts/persona_memory_tone_check.md` 手工记录）
