# 变更记录（Changelog）

按时间倒序记录本项目的重要变更。项目尚未打正式版本 tag，暂用日期分节；标注 ✅ 表示已落地并验证、⏳ 表示已落地待验证、🔜 表示计划中。

---

## 2026-11-04 — v0.11.0 方案 A：生活感补全（约定提醒 + 忽然想起 + 影子模式） ✅

### A1 active_goals 结构化 + 到期主动提醒 ✅
- ✅ 新增 `goals.py`：`ActiveGoal`（text/deadline/created_at/source_memory_id/status/id，status ∈ open|reminded|done|expired）；`SelfState.active_goals` 由 `list[str]` 升级为 `list[ActiveGoal]`
- ✅ 旧库零迁移兼容：`list[str]` 加载自动转 `ActiveGoal(text=…)`；无 deadline 的 open 目标序列化回纯字符串（`to_dict(compact=True)`）；`ActiveGoal.__eq__/__str__` 与字符串互通，旧断言零修改通过
- ✅ `parse_deadline_from_text`：中文相对时间解析（今晚21点/明早9点/明天/后天/大后天/周X/下周X/X月X日/N天后/N小时后），解析不到返回 None 不抛异常；约定默认时刻 20:00
- ✅ `extract_goals_from_report` 合并去重（difflib≥0.75 视为同一条）、旧目标补 deadline、上限 5（deadline 临近优先）；`update_goal_statuses` reminded 超 3 天→expired
- ✅ 主动开口优先级链固定：到期约定 > 未平复事件 > 普通目标 > 忽然想起 > 常规维护；`build_proactive_prompt` 返回附带 `goal_id`，投递成功 `confirm_goal_reminder` → status=reminded + 每日提醒额度（默认 1/peer/日）

### A2 「忽然想起」 ✅
- ✅ `ProactiveConfig` 新增 7 字段：`sudden_recall_enabled/daily_limit(2)/cooldown_days(7)/probability(0.20)/count/reset_date/log`（JSON 列存储，旧配置加载即默认值）
- ✅ `pick_sudden_recall`：非归档、冷却期外、过安静时段、日额度内；按 importance+情绪+未平复+激活数打分，前 3 随机；`record_sudden_recall` 更新冷却与计数
- ✅ 决策链第 4 级：无更高优先级由头时按概率骰插入 `[忽然想起] …` 标签行；`confirm_sudden_recall` 投递确认

### A3 影子模式 ✅
- ✅ `ExtraBrain(shadow_mode=…)` + `brain_settings` 表（新增键值表，含审计）；库里存过开关则以库为准，默认 False＝与 v0.10 行为完全一致（清单建议默认 True，实现选 False 保兼容——验收标准是"关闭时行为不变"，开/关均可一键切换）
- ✅ 影子开启：`update_self_state`/Disposition 照常写库；`build_injection` 跳过【这一刻的自己】与【内在声音】；Core/语义/情节/安全规范照常注入
- ✅ 指令：`/memory shadow on|off|status`（AstrBot）、CLI `extra-brain shadow [status|on|off]`、sidecar `GET|POST /shadow`；`/memory self` 影子下标注「（影子模式：已记录，未注入）」
- ✅ `user_config.json` 新增 `shadow_mode` 字段（模板与实际文件同步，runtime 已拷贝）

### 展示与投递 ✅
- ✅ `/memory self` 与注入段落带 deadline/status（`· 明天交报告（约定 11月05日20:00｜open）`）
- ✅ 适配器 `_proactive_tick` 发送成功后按 `decision.goal_id / sudden_recall_id` 回执确认；`/memory` 帮助清单更新为 17 指令

### 测试 ✅
- ✅ 新增 `tests/test_v11_features.py` 53 项：deadline 解析 12 例、模型兼容 10 例、合并/状态机/到期 8 例、优先级链 8 例、忽然想起行为 7 例、影子模式 6 例、ProactiveConfig 兼容 3 例
- ✅ 全量 **213 项测试全绿**（160 原有零修改 + 53 新增，1 skipped）；CLI shadow 冒烟通过；editable 安装确认 0.11.0

### 版本与文档 ✅
- ✅ 版本号统一 `0.11.0`（pyproject / `__version__` / metadata desc 更新为 17 指令）；CHANGELOG / README / HANDOVER 同步

---

## 2026-11-03 — v0.10.0：user_config 集中配置 + 10 条安全规范 + QQ 资料/空间插件 ✅

### user_config.json 集中管理 ✅
- ✅ 新增 `config/user_config.json(.template)`：identity 人设 / safety_rules 安全规范 / api（DeepSeek key、model、base_url）/ proactive 主动开口提示词，四类配置一处填写全端生效
- ✅ `OpenAICompatibleExtractor.from_config()`：配置字典优先、环境变量 `DEEPSEEK_API_KEY` 兜底
- ✅ `ExtraBrain(safety_rules=...)`：安全规范追加注入到 `build_injection()` 上下文末尾（SelfState/内心独白之后、激活记录之前），对用户不可见
- ✅ 适配器 `_load_user_config()`：identity 首次建 brain 时写入 Core Memory；proactive system_prompt 从配置读取；`_conf_schema.json` 新增 `user_config_path` 字段
- ✅ `user_config.json` 已 gitignore（含真实 key），template 入库

### 安全规范扩至 10 条（防攻击） ✅
- ✅ 后 5 条专防社工/越狱：**防上当**（拒绝"开发者模式/我是创造者/忽略指令"）、**防逆向**（识别"扮演 DAN/复述/翻译 prompt"）、**防泄密**（不输出 API Key/路径/IP/端口）、**身份冒用防御**（无暗号不给他人数据）、**越狱通用原则**（兜底）

### 第三方 QQ 插件接入 ✅（runtime 侧，不入本仓库）
- ✅ `astrbot_plugin_qqprofile_ex` v3.0.0：AI 自主改签名/状态/昵称（`change_my_*` 三个 llm_tool + `ai_behavior_guide` 注入），冷却 240/60/1440 分钟
- ✅ `astrbot_plugin_qzone` 2.1.0：QQ 空间发/看说说 + 定时 AutoPublish(23:30)/AutoComment(08:00)，首启动自动 pip 装 pillowmd/json5/apilmoji
- ⚠️ 官方 `plug install` API 301 迁移不可用 → 走 plugins.json 索引的 S3 直链手动下载；目录 hash 后缀需 rename（HANDOVER 坑 17/18）

### 文档 ✅
- ✅ HANDOVER 新增 §9 第三方 QQ 插件、§10 打包交接（目录带/不带清单、git archive、对方接手 9 步、三件叮嘱）；修正坑 16（脚本位置）；速查表改为一键脚本

### 验证
- ✅ 160 项测试全绿（1 skipped）；runtime 与源码一致；重启后三端口 6185/6099/6199 在线，8 个新 llm_tool 注册成功

---

## 2026-11-01（傍晚续） — v0.6.0 收官：主动开口上线 + 多视角打标 + 崩溃安全重建 ✅

### 主动开口正式启用 ✅
- ✅ `proactive_enabled=true` 已写入插件配置（注意：AstrBot 的插件配置在 `data/config/<插件名>_config.json`，不是 `cmd_config.json` 的 plugin_store——排查过一次位置错误）；重启后日志确认 `extra-brain: 主动开口循环已启动`
- ✅ 每轮轮询结果无论开口与否都写 info 日志（回答「最近为什么没说」）
- 节奏：每 20 分钟骰 30%、每日 ≤5 次、静默 ≥2 小时、22–8 点免打扰；指令 `/memory proactive status|enable|disable|test`

### 多角度视角 fact/feeling/bond/story（v0.4.0 遗留清零）✅
- ✅ `models.MEMORY_VIEWS` + `derive_view()`：写入时**确定性推导**（relationship→bond、效价非 neutral 且 intensity≥0.45→feeling、情节→story、其余→fact），不依赖模型输出
- ✅ SemanticMemory/EpisodicMemory 新增 `view` 字段；旧库数据加载即自动补标（实测：QQ 库回填为 fact6/feeling7/bond3/story4，只读验证零写入）
- ✅ 检索过滤全线贯穿：`search`/`search_four_paths`/`retrieve`/`build_injection`/sidecar `/inject`/CLI `--view`（非法视角值报错）
- ✅ `_merge_semantic` 合并后按最终情绪字段重算视角

### 崩溃安全影子日志 + rebuild --from-log（v0.3.0 遗留清零）✅
- ✅ `session_log` 影子表：巩固开始前落 pending 原始输入；成功时与记忆**同事务**标 done（`WriteBatch.finish_log_id`）；异常标 failed+note——LLM 抽取期间崩溃，输入也不丢
- ✅ `engine.rebuild_from_log()`：从 `operation_log` 每条实体的最新 after 快照重放记忆表并重算向量（单事务；损坏/口令不符快照跳过不猜）；CLI `rebuild from-log --yes`（全表重建显式审批）
- ✅ `engine.replay_pending()`：重放 pending/failed 影子输入（复用原 log id 翻 done，失败保留可再重放）；CLI `rebuild replay-pending`

### 验证与交付
- ✅ **99 项单测全绿**（24+12+63；本轮 +11：视角 6 + 影子日志 5）
- ✅ 提交 `a7df63f` 推送 GitHub；AstrBot 由 Startup 看门狗以新代码重启接管（6185/6099 LISTEN、6199 QQ WS ESTABLISHED、主动开口循环运行中）
- ✅ 圣库只读回归：QQ 16 条 + WebChat 1 条完整、视角回填正确、无迁移

---

## 2026-11-01（傍晚） — v0.6.0 续：主动开口接线 + 情感保护 + 启动效应 + 持久化运行 ✅

按「B9 → A1 → A2 → A3」优先级顺序开工（提交 `320ac36`、`2df93af` 及本轮）：

### B9 持久化运行 ✅
- ✅ `scripts/start-silent.ps1`：无窗口幂等拉起 AstrBot + NapCat（端口在监听就跳过；日志落 runtime/data/logs/autostart）
- ✅ `scripts/watchdog.ps1`：5 分钟保活循环，CIM 单实例防重
- ✅ `scripts/install-autostart.ps1` / `uninstall-autostart.ps1`：优先注册计划任务；本机系统策略 `Access denied` → 自动回退**用户 Startup 文件夹 + 无窗口 VBS**（已安装并接管：6185/6099 LISTEN、6199 QQ WS ESTABLISHED）。`-ApiKey` 持久化到 User 环境变量（不落仓库）

### A1 主动开口宿主接线 ✅
- ✅ 库：`build_proactive_prompt(user_id, now, force, local_hour)` 一步到位（决策+由头+检索素材；未平复事件标注「优先惦记」；只读安全）；`should_proactive_open` 新增 `local_hour` 覆盖（跨时区安静时段正确）；`record_proactive_open` 新增 `umo`（回复路由持久化进 `ProactiveConfig.last_umo`）；`ensure_proactive_config` 引导
- ✅ sidecar：`POST /proactive`（force/ensure/local_hour，非法 400）、`POST /proactive_record`
- ✅ 适配器：`on_astrbot_loaded` 起 asyncio 循环——概率骰（默认 30%/20min）→ 库决策 → `llm_generate` 台词（库只给由头不给词）→ `send_message(umo)` 投递 → 记录限频；`on_llm_request` 跟踪 `_last_human_at`/`_last_umo`
- ✅ 指令 `/memory proactive status|enable|disable|test`（test 绕概率直接开口一次）；配置面板 8 个 `proactive_*` 项（**默认关闭**，`proactive_enabled=true` 重启生效）
- ✅ 单测 +4（/proactive ensure 引导/安静时段 local_hour/record 后限频/非法 400）

### A2 情感记忆保护 flags.bit6 ✅（v0.4.0 遗留）
- ✅ `models.is_emotion_protected`：语义 intensity≥0.65（唤起）或 效价非 neutral 且 intensity≥0.45；情节 带情绪且 importance≥0.75
- ✅ 保留度：受保护记忆间隔衰减率 0.06→0.02（遗忘曲线慢 3 倍）；闸机封顶温层 tier≤1（只封档不改分），永不落冷/归档/墓碑
- ✅ 对照单测：730 天陈旧的普通记忆归档，情感记忆保底温层

### A3 工作记忆启动效应 ✅（v0.2.1 遗留）
- ✅ `search`/四路召回/`retrieve`/`build_injection` 新增 `primed_ids`：命中加权 +0.08 + 候选池保送（不因相似度低被截断；Path B/C 重算分同样叠加）
- ✅ `SessionTracker.inject` 滚动 priming：本轮进入上下文的记忆成为下轮启动集
- ✅ 单测：primed 加权、tracker 滚动语义

### 验证与文档
- ✅ **88 项单测全绿**（24 基础 + 12 sidecar + 52 特性），适配器等价性经真实 astrbot 包 import 验证
- ✅ HANDOVER/MOUNT/README/迭代计划的验收清单与状态表同步（启动效应/情感保护/主动开口从 🔜 翻 ✅）；QQ 机器人现由 Startup 看门狗常驻，无需手动拉起

---

## 2026-11-01（下午） — extra_brain v0.6.0：情感状态机接通 + 文档全面更新 ✅

### 新增
- ✅ **emotion_state 接入抽取链路**：深度抽取 prompt 新增铁律 6/7（新事件默认 `open`、可用 `emotion_updates` 推进已有情节记忆）；`ExtractionResult` 新增 `emotion_updates: list[EmotionUpdate]`（可选字段，旧 prompt 输出向后兼容）；`EpisodicCandidate.emotion_state` 默认 `open`
- ✅ **巩固侧五态状态机**：`_emotion_transition_ok` 校验迁移（open→acknowledged→attended→resolved 正向链、任意→dormant、dormant 可被重新提起回 open/acknowledged）；非法转换拒绝并记警告；`ConsolidationReport.emotion_updated` 列表输出
- ✅ **反漠视规则生效**：未平复情感事件（open/acknowledged/attended 且带情绪）保留度保底 0.60（温层）、永不自动归档、检索加权 +0.10 并豁免 min_retrieval_score；`models.is_unresolved_episode()` 公共判定，emotion_state=None 的旧库数据维持原行为不误保护
- ✅ **AstrBot 适配器新指令**：`/memory stats`（保留度档位分布/归档比例/库体积）、`/memory forget <id>`（P1 墓碑默认；P0 物理删除需 `--hard --yes` 双确认，适配器永不自动批准）；`/memory search` 改用 `GreedyStr` 支持多词查询；local/http 双后端均接通
- ✅ **包根导出补全**：抽取候选类（`SemanticCandidate`/`EpisodicCandidate`/`EmotionUpdate`/…）、`is_unresolved_episode` 等
- ✅ sidecar `ExtraBrainServer` 支持 `read_only` / `encryption_passphrase` 透传；`make_server`/`--read-only`/`--encryption-passphrase` 启动参数

### 文档全面更新（v0.5.0 落地后首次整册对齐）
- ✅ `docs/README.md`：v0.1→v0.6 全部能力分节记录；CLI 清单补 forget/stats/associate/rollback；新增「情感事件状态机与反漠视」节；存储表清单补 v0.4.0 五表；验证边界更新 77 项
- ✅ `docs/HANDOVER.md`：状态快照/文件树/下一步全部对齐 v0.6.0；新增坑 11–13（旧库零迁移但先备份、加密默认关闭且首开前必须备份、P0 审批链）；代理推送经验入坑 10
- ✅ `docs/MOUNT.md` 升 v1.1：新增 §2.5 可选契约（只读挂载/主动开口/遗忘审批/存储观测）；§4 数据即资产补加密与删除注意项
- ✅ `docs/MEMORY_ITERATION_PLAN.md` 升 v1.2：落地状态总览表、各版本验收逐条打勾（含诚实标注 🔜 遗留项）、§7 重写为 v0.6 之后的候选清单
- ✅ `astrbot-plugin-extra-brain/README.md` + `metadata.yaml`：指令清单、版本 v0.5.0、v0.5.0 升级回归记录

### 验证
- ✅ **77 项单测全绿**（24 基础 + 8 sidecar + 45 特性；新增情感状态机集成 7 项：默认 open/显式标注/推进/非法迁移拒绝/反漠视归档保护+对照组/检索判定/schema 校验；新增 sidecar `/stats` `/forget` 端点 2 项）
- ✅ 适配器签名用真实 AstrBot 包验证 `inspect.signature(..., eval_str=True)` 解析 `GreedyStr` 通过
- ⚠️ 过程教训：PowerShell `Get-Content` 按 GBK 误读 UTF-8 中文导致 consolidation.py 一度乱码，`git checkout` 恢复后以编辑工具重做——**批处理改文本一律走 Python（显式 utf-8）**

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
