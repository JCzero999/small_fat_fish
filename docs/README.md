# Extra Brain（额外大脑）

依据《长期记忆「额外大脑」架构规范 v0.9》实现的 Python 长期记忆库。面向陪伴型 Agent，重点是**少量、高相关、可解释、可遗忘**的记忆，而不是保存全部聊天原文。

> **文档索引**：设计规范见 `长期记忆「额外大脑」架构规范 v0.9.md`（第 10 节为实现对照）；DSH 宿主接入与跨会话验证见 `HANDOVER.md`；变更记录见 `CHANGELOG.md`；DSH 插件细节见 `../dsh-plugin-extra-brain/README.md`；**AstrBot 挂载（当前主力）见 `../astrbot-plugin-extra-brain/README.md`；挂载到任意宿主的通用契约见 `MOUNT.md`**。本文档位于 `docs/` 目录。

## 已实现

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

运行时只使用 Python 标准库，不依赖外部向量服务。

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
extra-brain --db memory.db consolidate conversation.txt
extra-brain --db memory.db search "查询" --entity "实体"
extra-brain --db memory.db inject "查询" --context-file recent.txt --token-limit 1800
extra-brain --db memory.db list semantic
extra-brain --db memory.db list episodic --include-archived
extra-brain --db memory.db list style
extra-brain --db memory.db protect sem_20261029_xxx
extra-brain --db memory.db protect sem_20261029_xxx --remove
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

衰减采用：

```text
importance * exp(-λ * days_since_last_activated)
* (1 + 0.1 * log(1 + activation_count))
```

结果限制在 `[0, 1]`。保护记忆固定为 `1.0`；默认 λ 为 `0.06`，低分且 90 天未激活的记忆归档，不再参与检索。

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
brain = ExtraBrain("memory.db", config=config)
```

Token 数使用对中英文较保守的内置估算，不绑定某个模型 tokenizer。生产环境若需要与特定模型完全一致，可在调用 `build_injection` 时设置更小的预算余量。

默认 `LocalHashingEmbedder` 是确定性的本地特征哈希，不需要下载模型；它不是神经网络语义 embedding。需要更强的跨语言表达召回时，实现 `EmbeddingProvider` 协议并注入：

```python
brain = ExtraBrain("memory.db", embedder=your_provider)
brain.reindex()  # 更换 provider 后必须重建索引
```

自定义抽取器实现 `MemoryExtractor.extract(prompt)` 即可；应返回经过 `ExtractionResult` 校验的数据，不能绕过深度抽取 Prompt。

## 存储与审计

所有数据位于一个 SQLite 文件中：

- `core_memory`
- `semantic_memories`
- `episodic_memories`
- `style_memories`
- `memory_vectors`
- `operation_log`

每次巩固使用一个事务。Core 超限、向量生成失败或数据库写入失败时，本批记忆、向量和审计记录不会部分提交。`operation_log` 保存 before/after 快照，便于定位问题及按快照人工恢复。

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
# 终端 2
node dsh-plugin-extra-brain/scripts/bridge-smoke.mjs http://127.0.0.1:8756
```

DSH 是 Developer Preview；AstrBot 为活跃维护的成熟框架（支持 QQ 等 15+ 平台）。完整安装与跨会话验证步骤见 `HANDOVER.md`，变更记录见 `CHANGELOG.md`。

## 验证边界

测试覆盖数据校验、持久化、空抽取与闲聊过滤、去重、冲突修正、衰减与保护、自动晋升、事务回滚、加权检索、注入预算和跨会话情绪记忆召回，以及 sidecar HTTP 服务（per-peer 隔离、错误码、只读降级）共 30 项。真实 DeepSeek 请求需要调用方提供有效 API Key；离线测试不会访问外部服务。

跨天闭环（`scripts/demo_closed_loop.py`）与真实 DeepSeek 已验证：改口检出（`conflicts`）、语义降权（`conflict_flag` + 置信度减半）、Core 清理（`core_pruned`）在多日场景下整条链路正常。

宿主 DeepSeek Harness / QQ 的消息监听、会话持久化和定时任务不在本库内；宿主只需在会话结束或定时窗口调用 `consolidate`，在生成回复前调用 `build_injection`。
