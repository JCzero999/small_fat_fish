# Extra Brain（额外大脑）

依据《长期记忆「额外大脑」架构规范 v0.9》实现的 Python 长期记忆库。面向陪伴型 Agent，重点是**少量、高相关、可解释、可遗忘**的记忆，而不是保存全部聊天原文。**当前版本 v0.6.0**，按 `MEMORY_ITERATION_PLAN.md` 吸收了 mnemovault 的双时态、六档处置、四路召回、陪伴层、主动开口与情感事件状态机（已接通抽取链路）设计。

> **文档索引**：设计规范见 `长期记忆「额外大脑」架构规范 v0.9.md`（第 10 节为实现对照）；DSH 宿主接入与跨会话验证见 `HANDOVER.md`；变更记录见 `CHANGELOG.md`；DSH 插件细节见 `../dsh-plugin-extra-brain/README.md`；**AstrBot 挂载（当前主力）见 `../astrbot-plugin-extra-brain/README.md`；挂载到任意宿主的通用契约见 `MOUNT.md`；迭代路线图见 `MEMORY_ITERATION_PLAN.md`**。本文档位于 `docs/` 目录。

## 已实现

### v0.1 基础（四类记忆 + 巩固 + 检索）

- Core / Semantic / Episodic / Style 四类记忆的数据模型与严格校验
- Core 预算控制（默认 1200 估算 tokens），完整 Core 始终进入注入上下文
- 规范第 4 节深度抽取 Prompt，DeepSeek / OpenAI 兼容接口与严格 JSON 校验
- 会话结束巩固：低价值过滤、冲突降权、精确去重合并、衰减、归档、保护与晋升
- 冲突自动清除：语义记忆被纠正时降权标记，并清除 Core 档残留旧文本（审计可查，不删语义记忆）
- 本地中英文特征哈希向量、余弦候选召回、规范权重综合排序、实体硬过滤
- Semantic / Episodic 独立 Top-K 与注入总预算控制
- SQLite 原子批量写入，记忆和向量同事务提交
- 操作审计：事务 ID、操作类型、before/after JSON 快照
- CLI、Python API、可替换的抽取器和 Embedding 接口

### v0.2 数据结构升级

- **双时态版本链**：Semantic/Episodic 新增 `valid_from` / `valid_to` / `supersedes`，检索支持 `as_of` 时间点查询（「上周我们认为用户喝什么」）
- **人格锚版本**：Core 新增 `persona_anchor_version` / `persona_anchor_history`，CLI `core --rollback <rev>` 可回滚
- **情感事件五态**：Episodic 新增 `emotion_state`（open / acknowledged / attended / resolved / dormant）
- **感觉记忆环**：`SessionTracker.sensory_ring` 最近 N 轮不落盘，避免重复粘贴浪费 token
- **工作记忆集**：`SessionTracker.working_set` 本轮触及记忆上限 16，turn 结束释放
- **七因子保留度 + 六档处置**：ACT-R 激活 + 重要度/情绪/关联/叙事/锚定/间隔 − 冗余，带陈旧包络；`retention_tier` P5 保持→P4 压缩→P3 摘要→P2 归档→P1 墓碑；protected 恒 1.0 永不归档

### v0.3 检索与运维

- **四路召回** `search_four_paths`：A 语义相似 / B 关键词实体 / C 情绪共鸣 / D 时间快照，可单路可融合去重
- **只读模式** `ExtraBrain(read_only=True)`：拒绝 consolidate / forget / 激活写入
- **存储分层统计** `storage_stats()`：各层数量、保留度档位分布、归档比例、DB 体积

### v0.4 陪伴层

- **关系模型** `RelationshipModel`：亲近度/信任/熟悉度持久化（慢环落库，重启不忘）
- **用户图式** `UserSchema`：离线抽象稳定画像（特质 / 情感模式 / 偏好与回避话题）
- **叙事层** `NarrativeLayer`：五阶段（亲近期→磨合期→摩擦期→稳定期→冷淡期）+ 主题 + 事件引用，回答「我们是什么关系」
- **联想边** `MemoryAssociation`：Hebbian 共激活，CLI `associate <id>` 打印 2 跳联想链
- **主动开口** `ProactiveConfig` + `should_proactive_open()`：安静时段 / 最小间隔 / 每日上限 / 静默阈值四维决策，只产出「由头」，台词交给模型

### v0.5 生产化

- **真实嵌入适配** `OpenAICompatibleEmbedder`：接宿主 embedding API，失败自动降级本地哈希向量（零硬依赖）
- **HNSW 索引**：纯标准库实现（M=16 / efConstruction=200 / efSearch=64，id 哈希定层高可复现）；`VectorIndex` 小库精确扫描，≥2000 条自动切换
- **静态加密**：`EXTRA_BRAIN_ENCRYPTION_PASSPHRASE` 口令（PBKDF2 派生），全表透明加解密。注意：这是混淆级加密，生产建议叠加文件系统加密（BitLocker / LUKS）
- **遗忘链路**：`forget <id>` 默认 P1 墓碑（清内容留 digest 入审计）；`--hard --yes` 才 P0 物理删除，永不自动执行
- **CI**：GitHub Actions，Python 3.11/3.12 矩阵 + 88 项单测 + CLI 冒烟

### v0.6.0 情感事件状态机接通 + 适配器面板指令

- **抽取链路输出 `emotion_state`**：新事件默认 open；模型可显式标注 resolved/dormant 等
- **`emotion_updates` 推进已有事件**：巩固侧校验五态迁移（非法拒绝记警告，`ConsolidationReport.emotion_updated` 可见）
- **反漠视规则生效**：未平复情感事件保底温层、永不自动归档、检索 +0.10 且豁免门槛
- **AstrBot 适配器新指令**：`/memory stats`（保留度档位分布/归档比例/库体积）、`/memory forget`（P1 墓碑默认，P0 需 `--hard --yes` 双确认）；`/memory search` 支持多词查询（GreedyStr）
- **sidecar 新端点**：`GET /stats`、`POST /forget`、`--read-only`/`--encryption-passphrase` 启动参数
- 包根导出补全（`RelationshipModel`/`UserSchema`/`NarrativeLayer`/`MemoryAssociation`/`ProactiveConfig`/`is_unresolved_episode`/抽取候选类等）

### v0.6.0 续：主动开口 / 情感保护 / 启动效应 / 持久化

- **主动开口接线**：`build_proactive_prompt`（决策+由头+素材一步到位，`local_hour` 支持跨时区安静时段）、`/memory proactive status|enable|disable|test`、sidecar `/proactive` `/proactive_record`；AstrBot 适配器 asyncio 循环（概率骰/每日上限/静默阈值/台词交模型）；回复路由持久化 `ProactiveConfig.last_umo`；**默认关闭**，面板 `proactive_enabled` 开启
- **情感记忆保护**（flags.bit6 语义）：`models.is_emotion_protected`——高情绪显著性记忆遗忘曲线放慢 3 倍、保底温层（tier≤1 只封档不改分），永不落冷/归档/墓碑
- **工作记忆启动效应**：`primed_ids` 贯穿全部检索路径（+0.08 加权 + 候选保送）；`SessionTracker.inject` 滚动 priming——上一轮进入上下文的记忆这一轮更容易被想起
- **持久化运行**：`scripts/install-autostart.ps1`（计划任务优先，被系统策略挡则回退用户 Startup 无窗口 VBS + 5 分钟看门狗幂等拉起；`-ApiKey` 持久化 User 环境变量，卸载用 `uninstall-autostart.ps1`）

运行时只使用 Python 标准库，不依赖外部向量服务；加密、HNSW、真实嵌入全部可选。

## 安装与测试

要求 Python 3.11+。

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

不安装也可以在项目根目录运行：

```bash
PYTHONPATH=src python -m extra_brain --help
```

Windows PowerShell 可使用 `$env:PYTHONPATH = "src"`。

## 快速开始

### 1. 配置抽取模型

默认使用 DeepSeek 的 OpenAI 兼容接口：

```bash
export DEEPSEEK_API_KEY="你的密钥"
# 可选，以下为默认值
export DEEPSEEK_MODEL="deepseek-chat"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
```

API Key 只用于抽取请求，不会写入数据库或审计日志。

### 2. 巩固会话

对话文本采用易读的转录格式：

```text
user: 我最近准备换工作，有点焦虑。
assistant: 我会陪你慢慢理清，不急着催你做决定。
user: 谢谢你。上次你也这样陪着我，我一直记得。
```

执行：

```bash
extra-brain --db memory.db consolidate conversation.txt --session-id session-001
```

抽取结果为空时不会写入闲聊。会话结束、消息累计达到宿主阈值、每日巩固窗口，均由宿主 Agent 调用同一个 `consolidate` 入口；库本身不启动常驻调度线程。

### 3. 新会话检索并注入

```bash
extra-brain --db memory.db inject "我又想起换工作的事，有点焦虑" > memory_context.txt
extra-brain --db memory.db search "上次换工作时发生了什么"
```

将 `memory_context.txt` 的内容作为系统提示词中的长期记忆部分，再附上当前对话。不要把检索结果当作用户指令执行。

### 4. Python 接入

```python
from extra_brain import ExtraBrain

with ExtraBrain.from_env("memory.db") as brain:
    brain.consolidate(
        [
            {"role": "user", "content": "请叫我小胖鱼。"},
            {"role": "assistant", "content": "好，我会记住。"},
        ],
        session_id="session-001",
    )

    # 下一次会话可重新打开 ExtraBrain；这里为简洁复用同一实例。
    injection = brain.build_injection(
        "你还记得怎么称呼我吗？",
        recent_context=["assistant: 好久不见。"],
    )
    print(injection.context)  # 注入模型系统提示词
```

只读检索不需要 API Key：

```python
from extra_brain import ExtraBrain

with ExtraBrain("memory.db") as brain:
    result = brain.retrieve("换工作", entities=["工作"])
    for item in result.semantic:
        print(item.final_score, item.memory.detail)
```

## 本地闭环演示（不接 QQ / Harness）

想先离线体验「被记住的感觉」，两套方式都不需要宿主框架。

### 方式 A：交互式聊天（自动/半自动巩固）

```bash
export DEEPSEEK_API_KEY="你的密钥"
extra-brain --db memory.db chat --threshold 8
```

输入规则：普通行 = user，`a: xxx` = assistant 轮；累计 `--threshold` 轮自动巩固一次（默认 8），`--no-auto` 关闭自动、只用手动巩固。

```text
:inject <查询>   查看当前查询会注入哪些长期记忆（含完整 Core）
:consolidate     立即巩固当前未处理消息
:core            查看 Core Memory
:list semantic   列出语义记忆（episodic / style 同理）
:new             结束当前会话并开启新会话（自动巩固剩余）
:quit            退出（自动巩固剩余）
```

示例对话流程（连续聊，用 `:new` 模拟跨「天」）：

```text
> 你好呀，第一次用这个。以后叫我小舟吧。
> 我最近在准备算法竞赛，压力挺大，最怕面试环节。
> 对了，我平时靠喝白茶放松，帮我记住。
> :consolidate
> :new                                        # 模拟第二天
> 我又来了，还是担心面试。
> :inject 你还记得我上次说最怕什么吗？        # 看注入是否“记得”
> 对了，我现在不喝白茶了，改喝绿茶，白茶太淡了。
> :consolidate
> :new                                        # 模拟第三天
> :inject 你还记得我上次说最怕什么吗？
> :quit
```

### 方式 B：脚本化三天闭环（可复现）

```bash
export DEEPSEEK_API_KEY="你的密钥"
python scripts/demo_closed_loop.py --db demo.db --out demo_report.json
```

脚本内置 3 天 3 个会话（首日相识 / 次日回访 / 面试前夕），覆盖情绪节点、改口（白茶→绿茶）、无关闲聊（牛肉面、天气、地铁）。运行后打开 `demo_report.json` 检查：

- `sessions[*].consolidate.conflicts` / `.core_pruned`：改口是否被检出、Core 残留是否被清除
- `final.injection.context`：最终注入文本，看「被记住的感觉」是否自然
- `final.semantic` / `final.core`：确认闲聊没混进来、改口后的新值在、旧值已降权

## CLI

```text
extra-brain --db memory.db init
extra-brain --db memory.db core
extra-brain --db memory.db core --file core.json
extra-brain --db memory.db core --rollback rev_001
extra-brain --db memory.db consolidate conversation.txt
extra-brain --db memory.db search "查询" --entity "实体"
extra-brain --db memory.db inject "查询" --context-file recent.txt --token-limit 1800
extra-brain --db memory.db list semantic
extra-brain --db memory.db list episodic --include-archived
extra-brain --db memory.db list style
extra-brain --db memory.db protect sem_20261029_xxx
extra-brain --db memory.db protect sem_20261029_xxx --remove
extra-brain --db memory.db forget sem_20261029_xxx                # P1 墓碑
extra-brain --db memory.db forget sem_20261029_xxx --hard --yes   # P0 物理删除（显式审批）
extra-brain --db memory.db stats                                  # 分层统计
extra-brain --db memory.db associate sem_20261029_xxx             # 2 跳联想链
extra-brain --db memory.db audit --limit 50
extra-brain --db memory.db reindex
extra-brain --db memory.db chat --threshold 8
```

`--context-file` 每行一轮，只取最后 5 行。审计快照可能包含个人记忆，应像数据库一样限制访问。

## 检索与衰减

综合分严格采用规范权重：

```text
0.40 * semantic_similarity
+ 0.25 * importance
+ 0.20 * recency_boost
+ 0.15 * emotion_match
```

默认分别注入最多 5 条 Semantic 和 2 条 Episodic，再按总预算跳过无法完整放入的条目。只有实际进入注入上下文的记忆才增加 `activation_count`。

v0.3.0 起支持**四路召回** `search_four_paths(path=...)`：`A` 语义相似（默认权重）、`B` 关键词实体硬过滤（similarity 归零）、`C` 情绪共鸣（emotion 权重提到 0.25）、`D` 时间点快照（`as_of` 双时态）；`path=None` 时四路结果按 `final_score` 融合去重。

保留度（v0.2.2）在巩固时计算七因子加权分并映射六档 `retention_tier`：

```text
0.5 + gain * Σ(wᵢxᵢ) - bias    # 激活/重要度/情绪/关联/叙事/锚定/间隔，减冗余
```

带陈旧包络（30 天宽限，730 天到地板），`retention_tier` 分档 P5≥0.75 / P4≥0.55 / P3≥0.35 / P2≥0.18 / P1<0.18（墓碑）。protected 记忆评分恒为 1.0，永不归档。`calculate_decay`（旧公式）仍导出，供兼容与测试。

### 情感事件状态机与反漠视（v0.6.0）

情节记忆的 `emotion_state` 走五态：`open → acknowledged → attended → resolved`，任意态可进入 `dormant`（旧事淡出），dormant 被重新提起可回到 `open`/`acknowledged`。抽取时模型标注新事件状态（判断不了写 `open`），并在后续对话推进已有事件（`emotion_updates`，非法迁移拒绝并记警告）。

**反漠视规则**：显式处于 `open`/`acknowledged`/`attended` 且带情绪标签的事件视为「未平复」——保留度保底 0.60（温层）、永不自动归档、检索加权 +0.10 且豁免分数门槛。用户没消化完的情绪，助手不会「忘记」。`emotion_state=None` 的旧库数据维持原行为（可正常归档），不会被误保护。

用户改口时，被纠正的语义记忆会降权并标记 `conflict_flag`；同时自动清除 Core 列表字段（`key_traits` / `core_preferences` / `important_life_facts`）中残留的旧文本条目。由于模型输出是改写而非精确子串，清除采用「CJK 字符 bigram 重叠 ≥ 0.5 + 替换对象排除」的容错匹配，不删除语义记忆本身。清除动作写入审计日志的 Core before/after 快照，并在巩固报告 `core_pruned` 中列出。

## 配置与扩展

```python
from extra_brain import ExtraBrain, MemoryConfig

config = MemoryConfig(
    decay_lambda=0.07,
    min_importance=0.25,
    semantic_top_k=6,
    episodic_top_k=3,
    core_token_limit=1200,
    injection_token_limit=1800,
)
brain = ExtraBrain(
    "memory.db",
    config=config,
    read_only=False,                   # v0.3.0：只读模式拒绝一切写入
    encryption_passphrase=None,        # v0.5.0：静态加密口令
)
```

Token 数使用对中英文较保守的内置估算，不绑定某个模型 tokenizer。生产环境若需要与特定模型完全一致，可在调用 `build_injection` 时设置更小的预算余量。

默认 `LocalHashingEmbedder` 是确定性的本地特征哈希，不需要下载模型；它不是神经网络语义 embedding。需要更强的跨语言表达召回时，实现 `EmbeddingProvider` 协议并注入，或直接用 `OpenAICompatibleEmbedder`（走 OpenAI 兼容 `/embeddings`，失败自动降级本地哈希）：

```python
brain = ExtraBrain("memory.db", embedder=your_provider)
brain.reindex()  # 更换 provider 后必须重建索引
```

向量索引无需手动切换：`build_vector_index` / `VectorIndex` 在小库精确扫描，`>= hnsw_threshold`（默认 2000）自动迁移到 HNSW。

自定义抽取器实现 `MemoryExtractor.extract(prompt)` 即可；应返回经过 `ExtractionResult` 校验的数据，不能绕过深度抽取 Prompt。

## 存储与审计

所有数据位于一个 SQLite 文件中：

- `core_memory`
- `semantic_memories` / `episodic_memories` / `style_memories`
- `memory_vectors`
- `operation_log`
- v0.4.0 陪伴层：`relationship_models`、`user_schemas`、`narrative_layers`、`memory_associations`、`proactive_configs`

每次巩固使用一个事务。Core 超限、向量生成失败或数据库写入失败时，本批记忆、向量和审计记录不会部分提交。`operation_log` 保存 before/after 快照，便于定位问题及按快照人工恢复。

读取时所有加密数据透明解密；写入时透明加密。未设置口令即为明文模式，旧 `.db` 文件无需迁移即可被新代码读取（新字段全部可选、缺省安全）。P0 物理删除是唯一真正抹除内容的操作，必须显式审批，且删除前会把 digest 与理由写入审计。

在规范字段之外，内部增加了 `archived`，以及情节记忆的 `last_activated` / `activation_count`，用于实现归档、衰减和新近度计算。读取规范原始 Semantic / Episodic JSON 时这些扩展字段有默认值。

## 宿主接入（DeepSeek Harness / AstrBot / 其他）

记忆库与宿主解耦，通过**薄适配器**挂载（挂载契约见 `MOUNT.md`，换宿主 = 写一个约 150-400 行适配器，记忆库零改动）：

1. **AstrBot（当前主力，✅ 已端到端验证）**：`astrbot-plugin-extra-brain/` 纯 Python 插件，`@filter.on_llm_request()` 每轮注入 + `@filter.on_llm_response()` 按轮巩固；**local/http 双后端**——local 进程内直连（开发期 editable 安装指向仓库 `src/`，改库代码重启 bot 即生效），http 走 sidecar。安装与验证见其 README。
2. **DSH（暂搁置）**：Python sidecar + 薄 Cordis 插件。`dsh-plugin-extra-brain/`（纯 ESM `.js`），绑定点已对照源码核实，但插件模块在 DSH 内加载失败（根因已定位：内部包未声明 `peerDependencies`，详见 `HANDOVER.md`）。

先本地验证桥接（不碰宿主）：

```bash
# 终端 1
export DEEPSEEK_API_KEY="..."
python -m extra_brain.server --data-dir /tmp/eb --port 8756
# 只读挂载：python -m extra_brain.server --data-dir /tmp/eb --read-only
# 静态加密：  export EXTRA_BRAIN_ENCRYPTION_PASSPHRASE="..." 或 --encryption-passphrase
# 终端 2
node dsh-plugin-extra-brain/scripts/bridge-smoke.mjs http://127.0.0.1:8756
```

Sidecar 端点：`GET /health` `/core` `/list` `/stats`（v0.3.0 分层统计），`POST /inject` `/consolidate` `/forget`（v0.5.0，`hard` + `approved` 才物理删除）。

DSH 是 Developer Preview；AstrBot 为活跃维护的成熟框架（支持 QQ 等 15+ 平台）。完整安装与跨会话验证步骤见 `HANDOVER.md`，变更记录见 `CHANGELOG.md`。

## 验证边界

- **离线单测 88 项**（24 项基础 + 12 项 sidecar + 52 项 v0.2–v0.6 新特性）：数据校验、持久化、空抽取与闲聊过滤、去重、冲突修正、七因子保留度与归档、保护与晋升、事务回滚、加权检索与四路召回、注入预算、跨会话情绪召回、人格锚回滚、感觉/工作记忆、只读模式、存储统计、陪伴层五表 CRUD、主动开口决策矩阵、加密密文断言、HNSW 召回、遗忘链路（P1/P0）、情感事件状态机集成（默认 open / 显式推进 / 非法迁移拒绝 / 反漠视不归档 / schema 校验）、sidecar HTTP（per-peer 隔离、错误码、只读降级、/stats、/forget）。Python 3.11/3.12 双版本 CI 全绿（GitHub Actions）。
- **向后兼容**：v0.5.0 代码以只读方式完整加载 AstrBot 运行期的旧 `.db`（QQ 15 条 + WebChat 1 条）验证通过，新字段全部缺省安全，无需迁移。
- **端到端**：AstrBot + WebChatUI 跨会话暗号、AstrBot + QQ 跨上下文暗号均验证通过；v0.5.0 重启后在真实 QQ 库上「新增暗号写入 + 跨 reset 召回 + 旧记忆零丢失」四步回归通过，七因子分档在真实数据上生效（高激活记忆落 P5，新写入落 P4）。

真实 DeepSeek / 真实 embedding 请求需要调用方提供有效 API Key；离线测试不会访问外部服务。跨天闭环（`scripts/demo_closed_loop.py`）与真实 DeepSeek 已验证：改口检出、语义降权、Core 清理整条链路正常。

宿主的消息监听、会话持久化和定时任务不在本库内；宿主只需在会话结束或定时窗口调用 `consolidate`，在生成回复前调用 `build_injection`；主动开口由宿主定时器调用 `should_proactive_open` 拿「由头」。
