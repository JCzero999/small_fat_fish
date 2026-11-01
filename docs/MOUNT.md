# extra_brain 挂载规范（Mount Spec）v1.1

> 本文档定义把 extra_brain 记忆库挂载到任意聊天宿主（AstrBot / DSH / 未来自研平台）的**通用契约**。
> 核心原则：记忆库是产品，宿主是可替换的挂载点。换宿主 = 写一个新适配器（约 150 行），**永不返工记忆库**。
> v1.1（2026-11-01）：适配 extra_brain v0.5.0。四项契约不变（向后兼容），新增可选契约见 §2.5。

## 1. 两个挂载面（都是一等公民）

| 挂载面 | 适用宿主 | 接入方式 | 现状 |
|---|---|---|---|
| **Python API** | Python 宿主（AstrBot、MaiBot、自研 Python 平台） | `from extra_brain import ExtraBrain` 进程内直连 | ✅ `astrbot_plugin_extra_brain`（local 后端） |
| **HTTP 协议** | 任意语言宿主（Node/Go/自研非 Python 平台） | `extra_brain.server` sidecar：`POST /inject` `/consolidate` `/forget`，`GET /health /core /list /stats` | ✅ `dsh-plugin-extra-brain`（JS 桥）已用；`astrbot_plugin_extra_brain`（http 后端）亦支持 |

sidecar 启动可选参数（v0.3.0+）：`--read-only`（拒绝一切写入，适合面板/doctor 挂载）、`--encryption-passphrase`（静态加密，也可用环境变量 `EXTRA_BRAIN_ENCRYPTION_PASSPHRASE`）。

## 2. 挂载契约（四件事）

任何宿主适配器只需翻译以下四项，**不得**把宿主特有概念（persona 体系、平台账号体系）写进记忆模型：

### 2.1 peer_id —— 稳定的记忆主体标识
- 一个 peer = 一个 SQLite 库（`<data_dir>/<safe_peer_id>.db`）
- 选取规则：**记忆主体是谁就按谁**——私聊按用户、群聊按群、WebChatUI 按登录用户（剥离每次新建对话变化的 conversation_id）
- AstrBot 实现：`unified_msg_origin` → 剥离 webchat 的 conv 段（见 `resolve_peer_id`）
- DSH 实现：`~/.dsh/.anonymous-user-id`（一台机器一用户）
- ⚠️ 反例：DSH 曾用 `session.id`，每新会话一个新库，跨会话完全失忆（HANDOVER 坑 1）

### 2.2 注入点 —— 每轮 LLM 调用前
- 输入：`query`（本轮用户消息）+ `recent_context`（近几轮 "role: content"，可选）
- 输出：注入文本（Core 块 + 检索命中的语义/情节记忆）→ 追加到 system_prompt（或等效位置）
- AstrBot 实现：`@filter.on_llm_request()` 钩子改 `req.system_prompt`
- DSH 实现：`agent/pre-step` 事件往 decision.messages 追加记忆消息

### 2.3 巩固点 —— 一批对话完成后
- 输入：`messages`（`[{"role","content"}, ...]`，user+assistant 成对）+ `session_id`（写审计用）
- 行为：深度抽取（一次 LLM 调用）→ 写记忆库
- ⚠️ **异步铁律**：巩固含 60s 级 LLM 调用，宿主**不得**在事件循环里同步等待（AstrBot 用 `asyncio.to_thread` + 后台 task；DSH 用 fire-and-forget）
- 触发时机（IM 没有会话结束概念，规范 §5.1 的等价形式）：
  1. 累计 N 轮自动巩固（AstrBot 默认 8，可配）
  2. 手动指令（`/memory consolidate`）
  3. 每日定时窗口（可选，宿主侧 cron）
- ⚠️ **防自引用**：注入的记忆文本不得再被当作用户消息巩固。AstrBot 的注入走 system_prompt 天然隔离；DSH 走独立消息需带 `source.kind === 'extra-brain'` 过滤

### 2.4 降级 —— 记忆系统故障不影响对话
- 注入失败 → 记 warning 日志，本轮照常对话（记忆是增强，不是依赖）
- 巩固失败 → 消息放回缓冲，下轮重试
- 无 API Key → 只读模式（注入可用，巩固 503/报错）

### 2.5 可选契约（v0.4.0+，按需接）

| 契约 | 接口 | 宿主责任 |
|---|---|---|
| **只读挂载** | `ExtraBrain(read_only=True)` / sidecar `--read-only` | 面板/诊断/解释器一律用只读，读取不改变激活计数 |
| **主动开口** | `should_proactive_open(peer_id)` → `(bool, reason)`；`record_proactive_open(peer_id)` | 宿主定时器（cron）轮询；返回 `True` 时用 `reason`+检索记忆当**由头**让模型生成开场白，禁止直接吐台词；记录一次开口 |
| **遗忘审批** | `forget(id)`（P1 墓碑，默认可自动）/ `forget(id, hard=True, approved=True)`（P0 物理删除） | 适配器**永不**自动传 `approved=True`；P0 必须由用户显式指令触发 |
| **存储观测** | `storage_stats()` / `GET /stats` | 可选展示保留度档位分布、归档比例（`/memory status` 平替） |

## 3. 适配器的「薄」标准

- 只做钩子 → 契约的翻译；**禁止出现记忆库领域词汇**（importance/decay/embedding/conflict…那些属于 extra_brain 核心的词汇）
- 领域操作全部通过后端接口：`inject / consolidate / core / list / search / stats / forget`
- 现成参照：`astrbot-plugin-extra-brain/main.py`（≈400 行含注释与指令）、`dsh-plugin-extra-brain/index.js`（≈150 行）

## 4. 数据即资产

- 换宿主 = 拷贝 `<data_dir>/` 下的 `.db` 文件（每库自包含：记忆+向量+审计）
- schema 变更需提供迁移故事；`operation_log` 的 before/after 快照可支撑人工恢复
- peer 文件名规则：安全字符化 + md5 短哈希（避免 `:` 等非法字符，跨平台一致）
- **v0.2–v0.5 新字段向后兼容、旧库零迁移**（新字段全部可选、缺省安全，建表 `IF NOT EXISTS`）
- ⚠️ **加密与删除是资产级操作**：首次给已有库开加密、或执行 P0 物理删除前，必须先备份 `.db`（连同 `.db-wal` / `.db-shm` 边车文件）。P1 墓碑保留 digest 与审计，是唯一可自动执行的降档

## 5. 新宿主接入清单（约 1 小时）

1. 找到宿主的三个钩子：LLM 请求前 / LLM 响应后 / 稳定会话标识（多数框架都有官方等价物）
2. 选后端：Python 宿主 local（开发期把 `extra_brain_src_path` 指向仓库 `src/`，改代码重启即生效）；非 Python 用 http
3. 实现 2.1–2.4 四项契约（抄现成参照）
4. 验证：不可猜暗号三步法——会话 A 说「记住暗号 X」→ 巩固 → **新会话 B** 问「暗号是什么」，答对 = 注入+巩固+peer 稳定三链路全通
5. 边界情况自测：无 key 只读、注入失败降级、巩固失败重试

## 6. 已知宿主绑定点对照表

| 宿主 | peer_id 来源 | 注入钩子 | 巩固钩子 | 适配器 |
|---|---|---|---|---|
| AstrBot + QQ（NapCat/OneBot v11） | `aiocqhttp:FriendMessage:<QQ号>` / `GroupMessage:<群号>`（天然稳定，零处理） | `@filter.on_llm_request()` | `@filter.on_llm_response()` + 阈值缓冲 | `astrbot-plugin-extra-brain/` ✅ **已验证（2026-11-01，QQ 私聊跨上下文暗号）** |
| AstrBot + WebChatUI | `webchat:FriendMessage:webchat!<user>!<conv>`（剥 conv 段） | 同上 | 同上 | 同上 ✅ 已验证（2026-10-31） |
| DSH (Cordis) | `getOrCreateAnonymousUserId()` | `agent/pre-step` (step==1) | `session/event` 累计 + `turn/end` 冲刷 | `dsh-plugin-extra-brain/`（模块加载坑待修，见 HANDOVER 坑 4） |
| 未来自研 | 自定（建议：登录用户 ID） | LLM 调用前拦截 | 响应完成后回调 | 照 §5 清单写 |

> QQ 侧的实证价值：换 IM 平台**只换了消息源**（NapCat → OneBot v11 → AstrBot），记忆库与适配器一行未改——这正是「宿主无关挂载」设计的目标形态。IM 的 `/reset`（清上下文）与记忆库的 peer（跨上下文持久）是正交的两层。
