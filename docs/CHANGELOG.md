# 变更记录（Changelog）

按时间倒序记录本项目的重要变更。项目尚未打正式版本 tag，暂用日期分节；标注 ✅ 表示已落地并验证、⏳ 表示已落地待验证、🔜 表示计划中。

---

## 2026-11-01 — extra_brain v0.2.0 → v0.5.0 五连跳迭代 ✅

按 `docs/MEMORY_ITERATION_PLAN.md` 路线图，一次会话内完成全部规划特性。单测从 30 项扩到 **68 项**，全绿；数据库新字段全部向后兼容（旧 `.db` 直接可读）。

### v0.2.0 — 双时态版本链 + 人格锚版本 + 情感状态机 ✅
- ✅ `SemanticMemory`/`EpisodicMemory` 新增 `valid_from`/`valid_to`/`supersedes`：事实可过期、可成版本链
- ✅ 检索支持 `as_of` 时间点查询：能回答「上周我们认为用户喝什么」而不仅是「现在」
- ✅ `CoreMemory` 新增 `persona_anchor_version`/`persona_anchor_history`，CLI `core --rollback <rev>` 人格锚回滚
- ✅ `EpisodicMemory.emotion_state` 五态状态机（open/acknowledged/attended/resolved/dormant）

### v0.2.1 — 感觉记忆 + 工作记忆 ✅
- ✅ `SessionTracker.sensory_ring`：最近 N 轮对话环形缓冲（默认 2 轮，不落盘）
- ✅ `SessionTracker.working_set`：本轮触及记忆 ID 集合（上限 16，turn 结束清空），为下轮启动效应铺路

### v0.2.2 — 七因子保留度 + 六档处置 ✅
- ✅ `_calculate_retention_score`：ACT-R 激活 + 重要度 + 情绪 + 关联 + 叙事 + 锚定 + 间隔，减冗余，带陈旧包络（30 天宽限 / 730 天地板）
- ✅ `retention_tier` 六档：P5 保持 / P4 压缩 / P3 摘要 / P2 归档 / P1 墓碑；P0 物理删除需显式审批（v0.5.0 落地）
- ✅ protected 记忆评分恒 1.0，永不归档

### v0.3.0 — 四路召回 + 只读模式 + 存储统计 ✅
- ✅ `search_four_paths`：A 语义相似 / B 关键词实体 / C 情绪共鸣 / D 时间快照，可单路可融合
- ✅ `ExtraBrain(read_only=True)`：拒绝 consolidate / forget / 激活写入
- ✅ `storage_stats()`：各层数量、保留度档位分布、归档比例、DB 体积

### v0.4.0 — 陪伴层：关系模型 / 用户图式 / 叙事层 / 联想边 / 主动开口 ✅
- ✅ `RelationshipModel`：亲近度/信任/熟悉度持久化，重启不忘（慢环落库）
- ✅ `UserSchema`：离线抽象的稳定画像（特质/情感模式/偏好与回避话题）
- ✅ `NarrativeLayer`：五阶段（亲近期→磨合期→摩擦期→稳定期→冷淡期）+ 主题 + 事件引用，回答「我们是什么关系」
- ✅ `MemoryAssociation`：Hebbian 共激活联想边，`extra-brain associate <id>` 打印 2 跳联想链
- ✅ `ProactiveConfig` + `should_proactive_open()`：安静时段/最小间隔/每日上限/静默阈值四维决策，只生成「由头」，台词交给模型
- ✅ 五张新 SQLite 表 + 全量 CRUD + 审计

### v0.5.0 — 真实嵌入 + HNSW + 静态加密 + 物理删除审批 + CI ✅
- ✅ `OpenAICompatibleEmbedder`：接宿主 embedding API，失败自动降级本地哈希向量（零硬依赖）
- ✅ 纯标准库 `HNSWIndex`（M=16/efConstruction=200/efSearch=64，id 哈希定层高可复现）；`VectorIndex` 小库精确扫描、≥2000 条自动切 HNSW
- ✅ 静态加密：`EXTRA_BRAIN_ENCRYPTION_PASSPHRASE` / `--encryption-passphrase`，PBKDF2 派生密钥，全表透明加解密（混淆级，生产建议叠加文件系统加密）
- ✅ P1 墓碑 `forget <id>`：内容清空、留 digest、审计可查「为什么忘了」；P0 `forget <id> --hard --yes` 物理删除需显式审批
- ✅ `GET /stats` + `POST /forget` sidecar 端点；`--read-only` 启动参数
- ✅ GitHub Actions CI：Python 3.11/3.12 矩阵 + 安装 + 全量单测 + CLI 冒烟
- ✅ `pyproject.toml` / `__init__.py` 版本升到 0.5.0，导出全部新 API

### 验证
- ✅ 68 项单测全绿（新增 38 项覆盖 v0.2–v0.5 每个特性，含加密磁盘密文断言、HNSW 召回、主动开口决策矩阵、CLI 回滚/遗忘/联想）
- ✅ QQ 记忆库数据库（`海豚-3311` 等暗号）在新代码下完整可读，字段缺省全部兼容
- ✅ 提交 `c1923c9`（v0.2.0）→ `bde8e4a`（v0.4.0）等已推 GitHub master（v0.5.0 推送见下）

---

## 2026-11-01 — QQ 通道接通并验证 ✅

AstrBot 之上接 QQ 个人号（NapCat + OneBot v11 反向 WS），记忆库与适配器**零改动**。

### 新增
- ✅ QQ 链路：NapCat Shell 4.18.41（QQ NT 9.9.33-52230）→ 反向 WS `ws://127.0.0.1:6199/ws` → AstrBot `aiocqhttp` 平台（API 动态添加，无需重启）；机器人 QQ 号 `1178469499`
- ✅ AstrBot 平台配置 `qq`（`data/cmd_config.json` platform 段，aiocqhttp 类型）

### 验证（QQ 私聊，跨上下文暗号法）
- ✅ 私聊说暗号「海豚-3311」→ `/memory consolidate` → 巩固写入（日志：`peer=qq:FriendMessage:1975824324 semantic=1 episodic=1`）
- ✅ `/reset` 清空对话上下文后提问 → 机器人正确答出暗号
- ✅ 记忆库三层证据齐备：语义（`用户/拥有QQ测试暗号/海豚-3311`）+ 活跃线程 + Core `important_life_facts`
- ✅ peer 隔离正确：`qq_FriendMessage_<QQ号>.db`（私聊按人）与 webchat 库互不干扰——**IM peer 规则（私聊按人/群聊按群）天然成立，无需适配器改动**

### 过程中排掉的坑（详见 HANDOVER §6）
- NapCat v4.18.42 启动即崩 `error code v2:-8`（上游已知回归 [#2145](https://github.com/NapNeko/NapCatQQ/issues/2145)）→ 回退 4.18.41
- 官方 launcher.bat 依赖注册表 QQ 路径（OneKey 安装无注册表项）→ 直接用 `NapCatWinBootMain.exe` + 环境变量启动
- NapCat 降级后 payload 子目录（native/node_modules/static/worker）需逐个补拷
- ✅ 启停脚本合并为综合版 3 个（`scripts/start-all.ps1` / `stop-all.ps1` / `status-all.ps1`），运行一次即可，带端口冲突检查与幂等保护；删除了 5 个独立脚本

---

## 2026-10-31（晚） — 挂载到 AstrBot，端到端跨会话验证通过 ✅

宿主路线从 DSH 切换到 AstrBot（用户决策：AstrBot 只是记忆库的第一个挂载点，重点是打磨可迁移的记忆库本身）。

### 新增
- ✅ `astrbot-plugin-extra-brain/`：AstrBot 薄适配器（纯 Python，`@filter.on_llm_request()` 注入 + `@filter.on_llm_response()` 按轮巩固 + `/memory` 指令组）
  - **local/http 双后端**：local 进程内 `import extra_brain`（editable 安装指向仓库 src/，改记忆库代码重启 bot 即生效）；http 复用 `extra_brain.server` sidecar
  - peer 规则：`unified_msg_origin` 剥离 webchat 的 conversation 段 → 同一 ChatUI 用户跨对话共享记忆（私聊按人/群聊按群，天然 IM 隔离）
  - 巩固走 `asyncio.to_thread` + 后台 task（含 60s 级 LLM 抽取不阻塞事件循环）；注入失败照常对话；巩固失败回缓冲重试
  - 修复适配器首版的 peer 解析 bug：webchat 的 umo 是三段式 `webchat:FriendMessage:webchat!user!conv`，`partition(":")` 只切一段导致每会话一个库（发现于跨会话验证：会话 B 答不出暗号 → 三个会话三个 .db → 修复正则后单库共享）
- ✅ `docs/MOUNT.md` 挂载规范 v1：两个挂载面（Python API / HTTP）+ 四项契约（peer_id / 注入点 / 巩固点 / 降级）+ 适配器「薄」标准 + 新宿主 1 小时接入清单 + 宿主绑定点对照表

### 验证（AstrBot 4.29.4 / Python 3.12 / WebChatUI / DeepSeek-flash）
- ✅ 插件加载、`/memory status` 指令、DeepSeek provider 配置
- ✅ **跨会话暗号验证通过**：会话 A 说「暗号蓝鲸-7749」→ `/memory consolidate`（抽取写入 `sem_...: 用户/记忆测试暗号是/蓝鲸-7749`）→ **新会话 B** 正确答出暗号；注入内容复现确认检索命中（非巧合）
- ✅ extra_brain 30 项单测在 Python 3.12 下全绿（进程内直连兼容性）

### 环境
- AstrBot conda env `astrbot`（Python 3.12），运行目录 `D:\projects_software\astrbot-runtime`（插件 junction 自仓库）
- dashboard：`http://127.0.0.1:6185`（密码见启动横幅 / `ASTRBOT_DASHBOARD_INITIAL_PASSWORD` 环境变量）

---

## 2026-10-31 — DSH 宿主插件收尾（进行中）

围绕「把 extra_brain 挂到 DeepSeek Harness（DSH）」的插件侧收尾。

### 修正
- **peer_id 用错**（根因级 bug）：原用 `session.id`，而 DSH 每「新开会话」就是一个新 session → 新 peer_id → 新 `.db`，记忆被按会话隔离，跨会话根本记不住。改为 `getOrCreateAnonymousUserId()`（`@deepseek-ai/dsh-anonymous-user-id`，持久化于 `~/.dsh/.anonymous-user-id`），跨会话稳定——一台机器一个用户 = 一个记忆库。
- **注入绑定用错**：`ctx.systemPrompt.section()` 只支持静态文本（返回 disposer），不能做「每轮动态注入」。改为 `agent/pre-step`（每轮 `step === 1`）+ `createUserMessage`。

### 重写
- 插件由 `.ts` 改为纯 ESM `.js`——DSH 只加载编译后的 `.js`，不加载 `.ts`。
- 补 `cordis.patch.yml`（insert `extra-brain`）+ `package.json` 的 `dsh.bundle.patch`，否则 DSH 不识别 bundle。
- 绑定点对照本机 DSH `app.asar` 源码核实：`agent/pre-step`、`session/event`、`agent/disposed`、`getOrCreateAnonymousUserId`。
- 新增宿主启动日志 `extra-brain: loaded (peerId=…)`，用于确认 host-only 插件已加载（它不出现在客户端插件列表）。

### 安装
- 手动安装绕过 pnpm：移除已从 npmjs 下架的 `dsh-our-free-model-fish`（否则任何 `pnpm add/install` 报 `ERR_PNPM_MALFORMED_METADATA`）；插件目录 junction 到 profile `node_modules`；profile `package.json` 的 `dsh.profile.bundles` 追加 `"dsh-plugin-extra-brain"`；profile `cordis.patch.yml` 写 `extra-brain` 配置。
- 清理 8756 端口两个 sidecar 的争用（Windows `SO_REUSEADDR` 允许双绑定）：杀掉一个冒烟残留 + 一个真服务，统一为一个 read-write sidecar（绝对 data-dir `D:/projects/small_fat_fish/extra-brain`）。

### 验证
- 设计三层**跨会话**验证方案（不可猜暗号），规避「同一会话 transcript 自带上下文」的假阳性。⏳ 待跑（当前被下方「插件未加载」阻塞）。

### 诊断（插件未加载）
- **现象**：重启 DSH 后插件 `apply()` 仍未执行——插件顶部（import 之后）的 `mkdirSync(eb-markers)` 只要模块被加载就会建目录，实测该目录不存在 → 模块根本没 load 起来（不是绑定点/scope 问题，那些此前已修对）。
- **根因**（已定位，待修复验证）：插件 `import` 了 `@deepseek-ai/dsh-anonymous-user-id` 与 `@deepseek-ai/dsh-llm` 两个内部包，却**未在 `package.json` 声明 `peerDependencies`**。DSH 用自定义模块解析器（`app.asar` 内 `profile-resolution-bootstrap.js`）加载 profile `node_modules` 里的 linked 包，`@deepseek-ai/*` 共享包**仅在包声明了对应 peer 时**才路由到宿主共享包；否则回退原生解析，而 profile `node_modules` 里根本没有这些内部包（`@deepseek-ai/` 下只有 `cosmokit`、`schemastery`）→ import 抛错。参照：能正常工作的 `dsh-better-sidebar` 把这些内部包都声明在 `peerDependencies` 里。
- **方案**：插件改为**自包含**——内联 `getOrCreateAnonymousUserId`（读写 `~/.dsh/.anonymous-user-id`）与 `createUserMessage`（`{ role:'user', content, source, id: randomUUID() }`），去掉全部 `@deepseek-ai/*` 裸导入；`package.json` 补 `peerDependencies` 兜底。

---

## 2026-10-30 — sidecar + 桥接 + 三天闭环

- ✅ 新增 `src/extra_brain/server.py` 本地 HTTP sidecar：`POST /inject`、`POST /consolidate`、`GET /health`、`GET /core`、`GET /list`；per-peer 一个 `<data_dir>/<peer_id>.db`；无 `DEEPSEEK_API_KEY` 时只读降级（`/consolidate` 返回 503）。
- ✅ 新增 `dsh-plugin-extra-brain/` Cordis 插件骨架 + `scripts/bridge-smoke.mjs`；Node↔Python 桥接用真实 DeepSeek 冒烟通过。
- ✅ `scripts/demo_closed_loop.py` 三天闭环联测通过：改口（Day1「喝白茶」→ Day2「改喝绿茶」）→ 语义降权（`conflict_flag` + 置信度减半）→ Core 残留清理（`core_pruned`）全链路正常；闲聊（牛肉面/天气/地铁）未进入记忆。
- ✅ 修复三处线程问题：空线程 ID、模型伪造线程 ID、线程碎片化（抽取 Prompt 携带已有 `active_threads`，三天合并为单线程）。
- ✅ Core 档冲突清除落地：语义记忆被纠正时自动清除 Core 列表字段残留旧值，采用 CJK 字符 bigram 重叠 ≥ 0.5 的容错匹配，写入审计 before/after。

---

## 2026-10-29 — 首版实现 + 真实联测

- ✅ 首版实现落地：Core / Semantic / Episodic / Style 四类记忆模型 + 校验、第 4 节深度抽取 Prompt、SQLite 单事务写入 + 操作审计、加权检索、衰减/归档/保护/晋升、CLI 与 `chat` 交互式闭环。
- ✅ 真实 DeepSeek 联测通过：深度抽取、新会话召回、改口冲突降权符合预期，注入 633 tokens（预算 1800 内）。
