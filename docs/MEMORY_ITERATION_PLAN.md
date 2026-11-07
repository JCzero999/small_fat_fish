# extra_brain 记忆库迭代文档

> 记忆库从 extra_brain v0.1.0 向生产级可迁移记忆系统演进的架构路线图。  
> 参考项目：[Rain13Sad/bcf-mnemovault](https://github.com/Rain13Sad/bcf-mnemovault)（DSH 长期记忆与情感插件包族）。  
> 文档版本：v1.4（2026-11-07 更新：v0.2–v0.5 主线全部落地；v0.6–v0.13 里程碑见 §7 与 `CHANGELOG.md`。当前 **v0.13.4**：独立 Chat Shell 主场 + `/v1` Agent API + 记忆互通 + 主人身份卡 + 自动巩固 + 统一人设与记忆使用规则。**316 项单测全绿**（2 skipped）+ CI + 前端状态机冒烟 + 真实 LLM 回归）  
> 适用范围：src/extra_brain 核心库 + astrbot-plugin-extra-brain 适配器 + dsh-plugin-extra-brain 适配器（搁置中）

> **✅ 落地状态总览**（明细见各版本节与 `CHANGELOG.md`）
>
> | 版本 | 状态 | 备注 |
> |------|------|------|
> | v0.2.0 双时态/人格锚/情感五态 | ✅ 已落地（`c1923c9`），情感状态机链路已于 **v0.6.0** 接通 | 抽取输出 emotion_state/emotion_updates、巩固迁移校验、反漠视保护全生效（99 测试覆盖） |
> | v0.2.1 感觉/工作记忆 | ✅ 已落地（`caf2cef`），启动效应已于 **v0.6.0** 接通 | sensory_ring / working_set；inject 滚动 primed_ids，上轮触及记忆本轮检索 +0.08 且候选保送 |
> | v0.2.2 七因子+六档 | ✅ 已落地（`a7d6597`） | 真实数据分档已验证（高激活 P5、新写入 P4）；闸机机制简化为 protected 恒 P5；P0 在 v0.5.0 |
> | v0.3.0 四路/只读/统计 | ✅ 已落地（`362ccc7`） | A/B/C/D 四路 + 融合；只读跳过激活写入；storage_stats 含档位分布与 DB 体积 |
> | v0.4.0 陪伴层 | ✅ 已落地（`bde8e4a`），两项遗留已于 **v0.6.0 收官** | 五张新表+CRUD+审计；主动开口四维决策 API 已接宿主机上线；多角度视角 / 情感记忆保护 ✅ |
> | v0.5.0 生产级 | ✅ 已落地（`7f09816`） | OpenAI 兼容 embedding（降级安全）、纯标准库 HNSW（≥2000 自动切）、静态加密、P1 墓碑/P0 审批、CI 3.11/3.12 |

---

## 1. 现状回顾

extra_brain 是一套给陪伴型 Agent 的跨会话长期记忆系统，核心理念是「少量、高相关、可解释、可遗忘」。已验证端到端链路：

- **AstrBot + WebChatUI**：跨会话暗号验证通过（2026-10-31）
- **AstrBot + QQ（NapCat + OneBot v11）**：私聊按 QQ 号隔离记忆，跨上下文暗号验证通过（2026-11-01）；**v0.5.0 升级后四步回归通过**（新增暗号写入 + 跨 reset 召回 + 旧记忆零丢失，2026-11-01）
- **单测**：99 项（24 基础 + 12 sidecar + 63 新特性），Python 3.11/3.12 均全绿，GitHub Actions CI
- **核心模块**：models.py / config.py / embeddings.py（含 HNSW）/ extraction.py / storage.py / consolidation.py / retrieval.py / session.py / engine.py / server.py / cli.py

现有架构的边界与局限：

- 记忆载体为文本 SPO 三元组 + JSON 序列化，存储冗余，无二进制定长布局
- 向量为本地特征哈希（384 维），缺乏真实语义模型接入路径
- 检索仅语义/情节两路，缺少身份/图/时间多路召回
- 注入 token 预算 1800，无像 mnemovault 那样的 ≈116 token 稳态注入
- 巩固为单一轮数阈值（默认 8 轮），缺少六档处置、陈旧包络、闸机机制
- 情感仅 emotion_valence（positive/neutral/negative/mixed），无 PAD 心境、人格锚、关系模型
- 无主动开口、无叙事层、无记忆联想、无多角度视角
- 无面板/可观测性，仅 CLI 指令，无法逐条核对「为什么记得/为什么忘了」

---

## 2. 与 mnemovault 对比分析

mnemovault 是给 DeepSeek Harness 的长期记忆与情感插件包族（TypeScript/Node.js），已验证端到端，184 条测试，10 个包，运行期零第三方依赖。以下是两者的详细对照。

### 2.1 核心维度对照表

| 维度 | extra_brain（当前） | mnemovault（参考） | 评价 |
|------|---------------------|-------------------|------|
| **记忆载体** | 文本 SPO 三元组 + 语义/情节/风格记忆，JSON 序列化 | 定长「晶胞」（MemCell），二进制布局：2048 位稀疏码 + int8 向量 + 有类型超边 + 压缩残差 | mnemovault 更紧凑，single cell 约 342 B |
| **存储格式** | SQLite + JSON，可读但冗余 | 单个 SQLite 文件，二进制 cell + 向量矩阵 + SDR 倒排 | mnemovault 存储更紧凑，查询更快 |
| **向量模型** | 本地特征哈希向量（384 维，无依赖） | 128 维 int8 向量（折叠自 256 维哈希，冷层 PQ32 量化） | mnemovault 可接入真实 embedding 服务，extra_brain 可沿用哈希作为 fallback |
| **检索方式** | 语义/情节两路，余弦相似度 + 重要性/新近度/情绪匹配加权 | 四路召回（语义/身份/图/时间）+ 4±1 槽位竞争，SDR 倒排索引 + int8 矩阵 | mnemovault 检索更丰富，稳态注入 ≈116 token/轮 |
| **注入 token 预算** | 1800 tokens | ≈116 tokens/轮（省约 90%） | mnemovault 对上下文窗口更友好 |
| **巩固触发** | 轮数阈值（默认 8 轮）、手动 | 离线双相巩固：NREM（抽象图式）/ REM（发散联想）+ 幂律遗忘曲线 | mnemovault 巩固更生物学启发，更适合长周期陪伴 |
| **情感模型** | emotion_valence（4 档）+ 情绪强度 | PAD 模型（评价→愉悦/唤起/支配）→心境→关系→人格锚，五态情感事件状态机 | mnemovault 情感更立体，可演化可回滚 |
| **关系模型** | 简单 relationship_changes | kind=5 关系模型 cell，重启后恢复，用户图式（deriveUserSchema） | mnemovault 关系可持久化，extra_brain 缺失 |
| **人格锚/版本** | Core Memory protected_memories | kind=4 人格锚 + 版本链，可演化可回滚（`/mnemo persona rollback <rev>`） | extra_brain 缺少版本回滚能力 |
| **叙事层** | 无 | kind=6 叙事层（亲近/磨合/摩擦期 + 主题 + 事件引用） | mnemovault 能回答「我们是什么关系」，extra_brain 只能回答「他喜欢什么」 |
| **作用域隔离** | per-peer SQLite（peer_id 隔离） | cell.scope + cell.session，语义/图式全局共享，情景按会话隔离 | mnemovault 隔离更精细，extra_brain 更粗粒度 |
| **主动开口** | 无 | mnemo-proactive 包：频率/静默/每日上限/由头选择 | extra_brain 被动响应，缺少主动关怀 |
| **测试覆盖** | 30 项（Python unittest） | 184 条（TypeScript，含 SDR 性质、隐私隔离、面板对拍、扫描式回归） | mnemovault 测试更完备，有 CI 断言 |
| **面板/可观测性** | 无（仅 CLI） | 七页签客户端面板（记忆总览/检索/整理/保留度/收件箱/情感状态/设置） | extra_brain 缺乏可观测性，无法逐条核对「为什么记得/忘了」 |
| **宿主绑定** | DSH（未加载）+ AstrBot（已验证） | 专为 DSH 设计，9 个 npm 组合包，cordis.patch.yml | extra_brain 宿主无关，已验证 AstrBot + QQ |
| **运行时依赖** | 纯 Python 标准库 | Node 内置 `node:sqlite` / `node:zlib`，零第三方运行时依赖 | extra_brain 部署更简单，与 AstrBot 同进程直连 |
| **去重机制** | 精确去重合并（CJK bigram 重叠 ≥ 0.5） | Jaccard/containment 确定性去重，冲突建版本链 | mnemovault 去重更确定性，extra_brain 中文场景已验证 |
| **删除机制** | 归档 + 保护列表，不物理删除 | 六档处置（P5 保持→P1 墓碑→P0 物理删除需显式 `--yes` 审批），墓碑释放残差 | mnemovault 删除有审批链，extra_brain 缺少物理删除审批 |

### 2.2 mnemovault 的精华（建议吸收）

1. **双时态版本链**：`cell.tInvalid` + `recall({asOf})`，能回答「上周我们当时认为用什么」，而不仅是「现在认为用什么」。
2. **感觉记忆（SensoryRing）**：TTL 2 轮、64 KB、不落盘，避免用户反复粘贴同一段日志的 token 浪费。
3. **工作记忆（WorkingSet）**：上限 16 条，turn 结束释放引用，启动效应：这轮刚摸过的东西下轮更容易被想到。
4. **七因子保留度 + 六档处置**：ACT-R 激活 + 重要度/情绪/关联/叙事/锚定/间隔/冗余，六档（P5 保持→P1 墓碑→P0 物理删除需显式审批），闸只封顶 tier 不改分。
5. **情感事件五态状态机**：open → acknowledged → attended → resolved / dormant，未获回应的情绪不衰减，强制进入注入预算，反漠视。
6. **神经调制四信号**：DA/ACh/NE/CORT 四个标量，只作用于全局增益（编码初值、图跳数、检索温度），不改打分公式。
7. **四路召回 + 4±1 槽位**：语义/身份/图/时间，合并打分，4±1 槽位竞争，稳态约 116 token/轮。
8. **人格锚 + 版本链**：100 轮后助手不会变成另一个人，可 `/mnemo persona rollback <rev>`。
9. **关系模型落盘**：慢环不落盘 = 重启就忘，关系模型作为 kind=5 cell 持久化。
10. **离线用户图式**：唯一真正的「学习」，把散落的情感反应抽象成「这个人是什么样」（kind=3 cell）。
11. **可复现的召回**：读侧没有任何生成步骤，召回确定性，可从快照逐字节重放。
12. **残差压缩**：brotli（node:zlib，零依赖）+ 轮级共享，存储成本可控。
13. **主动开口**：频率/静默/每日上限/由头选择，有由头才生成给模型的提示，台词由模型自己说。
14. **严格的接口契约**：`npm run check:manifests` 门禁，组合包契约检查，安装期不静默失败。

### 2.3 extra_brain 的优势（应保留，不随迭代丢弃）

1. **纯 Python 标准库**：零第三方依赖，部署极简，与 AstrBot 同进程直连无桥接成本。
2. **宿主无关挂载契约**：已有 `docs/MOUNT.md` 规范，已验证 AstrBot + QQ（NapCat）双端，换宿主仅需薄适配器。
3. **可解释的审计日志**：操作审计，事务 ID、before/after JSON 快照，可审计可回滚。
4. **CJK 友好抽取**：中英文特征哈希，CJK bigram 容错匹配，中文场景下去重效果好。
5. **Core Memory 预算控制**：1200 tokens 严格预算，完整 Core 始终进入注入上下文。

---

## 3. 迭代路线图

总体原则：**保留纯 Python 标准库和宿主无关挂载契约，逐步吸收 mnemovault 的关键机制，每个版本都有可验证的验收标准。**

```
v0.2.0: 双时态版本链 + 人格锚版本 + 情感事件状态机（核心数据结构变更）
    ↓
v0.2.1: 感觉记忆 + 工作记忆（SessionTracker 增强）
    ↓
v0.2.2: 六档保留度 + 七因子评分（consolidation.py 重构核心）
    ↓
v0.3.0: 四路召回 + 只读接口 + 存储比统计（retrieval.py 增强）
    ↓
v0.4.0: 关系模型 + 用户图式 + 叙事层 + 主动开口（陪伴层）
    ↓
v0.5.0: 真实 embedding + HNSW + 静态加密 + CI 自动化（生产级）
```

每个版本完成后需要：
1. 更新 `docs/CHANGELOG.md`
2. 如果接口契约有变更，更新 `docs/MOUNT.md`
3. 运行 `python -m unittest discover -s tests -v` 确保不破坏现有 30 项测试
4. 在 AstrBot + QQ 环境下跑一次暗号验证，确保端到端不回退

---

## 4. 各版本详细规格

### v0.2.0 — 核心数据结构升级（双时态 + 人格锚 + 情感状态机）✅

**目标**：让记忆具有「时间维度」和「人格稳定性」，解决同一事实随时间反复覆盖、助手人格漂移的问题。

| 项目 | 内容 |
|------|------|
| **双时态版本链** | Semantic/Episodic Memory 新增 `valid_from` / `valid_to` / `supersedes` 字段；`recall()` 支持 `as_of` 参数，能查询「某个时间点的记忆状态」 |
| **人格锚 + 版本链** | Core Memory 新增 `persona_anchor_version` 字段；`/core` 增加 `--rollback <rev>` 指令，可将人格回滚到历史版本 |
| **情感事件五态状态机** | Episodic Memory 新增 `emotion_state` 字段（open/acknowledged/attended/resolved/dormant）；未获回应的情绪不衰减，强制进入注入预算，反漠视 |

**验收标准**：
- ✅ 同一事实在不同时间点查询返回不同版本（双时态）——`as_of` 单测通过
- ✅ `/core --rollback <rev>` 可将人格锚回退到指定版本——CLI 单测通过
- ✅ 情感事件状态机转换符合 open → acknowledged → attended → resolved / dormant——字段/校验/持久化 ✅；抽取输出与迁移校验、反漠视规则已于 **v0.6.0** 接通（非法迁移拒绝记警告单测通过）
- ✅ 现有单测全绿（99/99），新增单测覆盖新增字段

---

### v0.2.1 — 感觉记忆 + 工作记忆（SessionTracker 增强）✅

**目标**：减少重复信息的 token 浪费，提升短期上下文相关性。

| 项目 | 内容 |
|------|------|
| **感觉记忆（SensoryRing）** | TTL 2 轮、64 KB、不落盘；SessionTracker 新增 `sensory_ring`，最近 2 轮对话不持久化，仅本轮注入参考 |
| **工作记忆（WorkingSet）** | 上限 16 条，turn 结束释放引用；SessionTracker 新增 `working_set`，本轮刚提及的实体/概念优先检索 |

**验收标准**：
- ✅ 同一轮内重复粘贴的长文本不重复进入语义记忆（环形缓冲天然限流）
- ⚠️ 工作记忆中的实体在下一轮检索中优先级提升——✅ **v0.6.0** 接通：`primed_ids` 加权 +0.08 并候选保送，SessionTracker.inject 滚动 priming
- ✅ turn 结束后感觉记忆与工作记忆自动清空，不落盘——单测通过

---

### v0.2.2 — 六档保留度 + 七因子评分（consolidation.py 重构核心）✅

**目标**：让遗忘成为可解释、可审计的决策，而非黑盒 TTL。

| 项目 | 内容 |
|------|------|
| **七因子评分** | ACT-R 激活 + 重要度 + 情绪强度 + 关联度 + 叙事角色 + 用户锚定 + 间隔效应 − 冗余；`score = 0.5 + gain · (Σwᵢxᵢ − bias)`，gain < 1 防止上半段饱和 |
| **陈旧包络** | `score × (0.15 + 0.85·clamp01(1 − (days−30)/700))`；30 天不折损，730 天到地板 |
| **六档处置** | P5 保持（≥0.75，热层）→ P4 压缩（≥0.55）→ P3 摘要化（≥0.35）→ P2 归档（≥0.18）→ P1 墓碑（<0.18）→ P0 物理删除（永不自动执行，需显式 `--yes` 审批） |
| **闸机机制** | 闸只封顶 tier、不改分数；pin/情感/人格锚/里程碑/被确证的一手事实/图式可禁止最后一步降档 |

**验收标准**：
- ✅ 记忆按七因子计算出 score，并正确映射到六档——真实 QQ 库分档已验证（高激活 P5 / 新写入 P4）
- ✅ P0 物理删除必须显式 `--yes` 审批，否则拒绝——`forget --hard`（无 `--yes`）返回失败（v0.5.0 落地）
- ⚠️ 闸机不抬分，只封顶 tier——简化实现：protected 记忆直接恒 1.0（P5），等价于「锚定禁止降档」
- ✅ 现有单测全绿（99/99），新增保留度单测通过

---

### v0.3.0 — 四路召回 + 只读接口 + 存储比统计（retrieval.py 增强）✅

**目标**：提升检索丰富度，让可观测性成为可能。

| 项目 | 内容 |
|------|------|
| **四路召回** | 语义/身份/图/时间三路新增召回，与现有语义/情节检索合并打分；4±1 槽位竞争决定注入哪几条 |
| **只读接口** | 面板/doctor/解释器一律走 `read_only` 模式，读取不改变访问时间/激活计数 |
| **存储比统计** | 计算并展示 `storage_ratio`、`bytes_per_cell`、`residual_share`；避免 mnemovault 曾踩过的测量错误（分母/分子口径不一致） |
| **崩溃安全重建** | `session_log` 影子日志 + `/memory rebuild --from-log`；晶格是派生层，坏了重放，不是数据丢失 |

**验收标准**：
- ✅ 四路召回结果与现有语义/情节检索向下兼容（`path=None` 融合，`search()` 原接口不变）
- ✅ 只读接口不改变 `last_activated` / 激活计数（`build_injection` 在 read_only 下跳过激活写入）
- ✅ 存储比统计数字与实际 SQLite 文件大小一致（`db_size_bytes` 直接 stat）
- ✅ `rebuild --from-log` 影子日志重建已于 **v0.6.0** 落地：`session_log` 影子表（巩固前 pending、同事务 done、异常 failed）+ `rebuild from-log --yes`（operation_log after 快照重放记忆表 + 重算向量，单事务）+ `rebuild replay-pending`（重放未完成输入）

---

### v0.4.0 — 陪伴层与主动开口 ✅

**目标**：从「记得住」走向「记得的是什么」，加入陪伴场景核心能力。

| 项目 | 内容 |
|------|------|
| **关系模型落盘** | kind=5 关系模型 cell，重启后恢复；慢环不落盘 = 重启就忘，此项修复 |
| **离线用户图式** | kind=3 cell，把散落的情感反应抽象成「这个人是什么样」 |
| **叙事层** | kind=6，把情感事件串成「我们那段日子（亲近/磨合/摩擦期）」+ 主题 + 事件引用 |
| **记忆联想** | Hebbian 共激活强化，同轮被一起想起的记忆之间长 ASSOC 边；`/memory associate <id>` 打印 2 跳联想链 |
| **多角度视角** | fact / feeling / bond / story 四个写入时算好的视角，检索时可按视角过滤 |
| **主动开口包** | 频率/静默/每日上限/由头选择，产出给模型的提示而非台词；面板显示「最近为什么没说」 |
| **情感记忆保护** | \|效价\|≥0.45 或 唤起≥0.65 的记忆打 `flags.bit6`：巩固期跳过情绪衰减、遗忘曲线放慢 3 倍、最多降到温层 |

**验收标准**：
- ✅ 关系模型在重启后仍可查询（五张表持久化 + 单测 roundtrip）
- ✅ 用户图式能持久化稳定画像（traits/emotional_patterns/preferred/avoided_topics）；「从散落情感反应自动抽象」需离线巩固调度 🔜（当前提供 API 由宿主/巩固调用）
- ✅ 叙事层能回答「我们是什么关系」（五阶段 + 主题 + 事件引用）
- ✅ 主动开口在静默时段不产生提示（quiet_hours/min_interval/daily_limit/silence_threshold 四维决策矩阵单测通过）；宿主定时器已接线并**上线运行**（v0.6.0 收官，AstrBot 循环 + `/memory proactive` 指令）
- ✅ 情感记忆保护（\|效价\|≥0.45 / 唤起≥0.65）已于 **v0.6.0** 落地：`is_emotion_protected` 衰减率 0.06→0.02、闸机封顶温层（未用 flags.bit6 位图，改用等价谓词判定，语义相同）
- ✅ 多角度视角（fact/feeling/bond/story）已于 **v0.6.0** 落地：`view` 字段写入时确定性推导（不依赖模型），检索/注入/sidecar/CLI 按 `views` 过滤，旧库加载自动补标

---

### v0.5.0 — 生产级与可扩展（长期）✅

**目标**：接近 mnemovault 的生产可用性，同时保持 Python 纯标准库优势。

| 项目 | 内容 |
|------|------|
| **真实 embedding Provider** | `EmbeddingProvider` 已有抽象，新增对宿主 embedding 服务的适配器（如 OpenAI embeddings），本地哈希向量作为 fallback |
| **HNSW 索引** | cell < 2000 时精确扫描，超过自动切 HNSW（M=16 / efConstruction=200 / efSearch=64），层高走 id 哈希保证可复现 |
| **静态加密** | 记忆库里是用户的真实对话，支持 SQLite 加密扩展或文件系统加密 |
| **物理删除审批** | `forget(hard)` 按 sha 检查引用后真删残差；`doctor` 报告并清理孤儿残差；永远要审批、无审批面直接拒绝 |
| **CI 自动化** | GitHub Actions：30 项单测 + 端到端暗号验证 + 存储比断言（storage_ratio ≤ 0.90） |

**验收标准**：
- ✅ embedding 接口可切换，本地哈希向量作为 fallback 不影响现有功能（`OpenAICompatibleEmbedder` 网络失败自动降级，单测覆盖）
- ✅ 1000+ cell 时召回延迟不显著劣化（纯标准库 `HNSWIndex`，`VectorIndex` ≥2000 自动切换；小库精确扫描、HNSW 召回单测通过）
- ✅ 数据库文件可加密，未授权进程无法直接读取明文（PBKDF2 口令派生 + 全表透明加解密，磁盘密文断言单测通过；XOR 混淆级，生产建议叠加文件系统加密）
- ✅ `forget --hard` 需要显式确认，否则拒绝执行（`--yes` / `approved=True` 双保险；P1 墓碑默认，digest 入审计）
- ✅ CI 在每次 push 时自动跑单测（GitHub Actions 3.11/3.12 矩阵 + 302 项单测 + CLI 冒烟）。端到端暗号验证与 storage_ratio 断言属于人工/集成环节，离线 CI 不访问外部服务与 LLM，故未纳入自动门禁

---

## 4. 数据结构变更预览（v0.2.0 起）

### SemanticMemory 新增字段

```python
@dataclass
class SemanticMemory:
    id: str
    type: str              # fact|preference|relationship|goal|trait
    subject: str
    predicate: str
    object: str
    detail: str
    importance: float
    emotion_valence: str
    emotion_intensity: float
    confidence: float
    # --- 新增（v0.2.0） ---
    valid_from: str        # ISO8601，生效开始时间
    valid_to: str | None   # ISO8601，生效结束时间（None 表示持续有效）
    supersedes: str | None # 已取代的记忆 ID，形成版本链
    retention_tier: int    # 0=热, 1=温, 2=冷, 3=归档, 4=墓碑
    persona_anchor_version: str | None  # 人格锚版本 ID
    emotion_state: str     # open|acknowledged|attended|resolved|dormant
```

### EpisodicMemory 新增字段

```python
@dataclass
class EpisodicMemory:
    id: str
    timestamp: str
    summary: str
    key_moments: list[str]
    emotions: list[str]
    importance: float
    # --- 新增（v0.2.0） ---
    valid_from: str
    valid_to: str | None
    supersedes: str | None
    retention_tier: int
    emotion_state: str
```

### CoreMemory 新增字段

```python
@dataclass
class CoreMemory:
    version: str
    last_updated: str
    identity: dict
    user_stable_profile: dict
    active_threads: list[dict]
    protected_memories: list[str]
    # --- 新增（v0.2.0） ---
    persona_anchor_version: str  # 当前人格锚版本
    persona_anchor_history: list[dict]  # 历史版本，支持 rollback
```

---

## 5. 风险与注意事项

1. **数据结构变更需迁移故事**：实测 v0.2.0 起所有新增字段均可选、缺省安全，旧 `.db` **直接可读、零迁移**（QQ/WebChat 真实库只读验证通过）。`rebuild --from-log` 与 `session_log` 重放用于派生层损坏/进程崩溃恢复，不是版本迁移通道。
2. **不要一次性引入太多机制**：建议严格按版本顺序实施，每个版本验收后再进入下一个。
3. **保留中文 CJK 友好性**：mnemovault 的 SDR/向量主要针对英文，extra_brain 的 CJK bigram 去重在中文场景有优势，吸收时不要破坏。
4. **保持宿主无关**：所有新增功能应在 `src/extra_brain` 核心库内实现，适配器层（AstrBot/DSH）只做钩子翻译，不渗入记忆领域词汇。
5. **数据即资产**：`extra-brain/`（2026-11-07 起在仓库下，此前为 astrbot-runtime\data\extra_brain）下的 `.db` 文件只能在最初初始化一次，之后不能随意删除或重新初始化；如需重新初始化，必须征得用户亲自确认。

---

## 6. 附录：mnemovault 关键概念映射表

| mnemovault 概念 | extra_brain 对应（v0.5.0 目标） | 备注 |
|-----------------|----------------------------------|------|
| MemCell（晶胞） | SemanticMemory / EpisodicMemory（定长化） | v0.2.0 起向定长布局靠拢 |
| SDR（稀疏码） | 本地特征哈希向量 | 可保留，或升级为 2048 位稀疏码 |
| int8 向量 | 本地特征哈希向量（384 维） | v0.5.0 接入真实 embedding 服务 |
| 有类型超边 | SemanticMemory.predicate + relationship_changes | v0.4.0 关系模型落盘 |
| 双时态 | valid_from / valid_to / supersedes | v0.2.0 核心功能 |
| 感觉记忆 SensoryRing | SessionTracker.sensory_ring | v0.2.1 |
| 工作记忆 WorkingSet | SessionTracker.working_set | v0.2.1 |
| 七因子保留度 | consolidation.py 七因子评分 | v0.2.2 |
| 六档处置（P5→P1） | consolidation.py retention_tier | v0.2.2 |
| 情感事件五态 | EpisodicMemory.emotion_state | v0.2.0 |
| 人格锚 + 版本链 | CoreMemory.persona_anchor_version | v0.2.0 |
| 关系模型 kind=5 | 新增关系模型 cell | v0.4.0 |
| 用户图式 kind=3 | 新增用户图式 cell | v0.4.0 |
| 叙事层 kind=6 | 新增叙事层 cell | v0.4.0 |
| 记忆联想 ASSOC | 新增 ASSOC 边 | v0.4.0 |
| 多角度视角 | fact/feeling/bond/story | ✅ v0.6.0 收官（view 字段确定性推导 + 全线过滤） |
| 主动开口 mnemo-proactive | 新增主动开口包 | ✅ 决策 API + 宿主定时器已接并上线（v0.6.0 收官） |
| 情感记忆保护 flags.bit6 | models.is_emotion_protected（等价谓词） | ✅ v0.6.0（衰减 3 倍慢 + 温层封顶） |
| HNSW 索引 | retrieval.py 向量索引升级 | v0.5.0 ✅（embeddings.py `HNSWIndex`/`VectorIndex`） |
| 静态加密 | SQLite 加密 / 文件系统加密 | v0.5.0 ✅（口令派生 + 透明加解密，混淆级） |
| 物理删除审批 | forget(hard) + --yes 审批 | v0.5.0 ✅ |
| CI 自动化 | GitHub Actions | v0.5.0 ✅（3.11/3.12 + 99 测试 + CLI 冒烟） |

---

## 7. 下一期候选（2026-11-07 更新）

> 本文件是 v0.2→v0.5 的主线路线图，已全部落地；**v0.6–v0.13 的后续迭代不在本文件展开，以 `CHANGELOG.md` 为准**。已完成里程碑：v0.6.0 情感状态机接通 + 主动开口上线 / v0.7.0 SelfState / v0.8.0 DispositionSeed / v0.9.0 疲劳完整化 + 人生快照 / v0.10.0 user_config + 10 条安全规范 / v0.11.0 生活感补全 / v0.12.0 FSRS 牢固度 / v0.12.1 表达与人设层 / v0.13.0 独立 Chat Shell + Agent API / v0.13.1 记忆互通 + 数据架构统一 / v0.13.2 主人身份卡 / v0.13.3 自动巩固改版 / v0.13.4 统一人设 + 记忆使用规则（唯一源 + 【使用约定】脚注）。剩余候选：

1. **方案 C：自主记账（v0.14.0）**：巩固产出候选记忆，经模型/用户确认后再正式写入（`CandidateMemory` + `AutonomyConfig`，默认关）。函数级清单见 `额外大脑 迭代函数级任务清单1104.md`
2. **用户图式自动抽象**：离线巩固从散落情感反应自动产出 `UserSchema`（建议真实数据积累一两周后做）
3. **每日定时巩固窗口**（规范 §5.1 第三触发；当前由适配器轮次阈值 + Shell 前端策略覆盖）
4. **群聊场景实测**：多人共享群库时的发言者归属与记忆边界（v0.13.2 已提供 `[发言者 …]` 标注）
5. 可选：真实 tokenizer 预算、http 后端多宿主共享部署、Shell 深色主题、AstrBot 主动开口与 Shell 统一调度

验收纪律：`python -m unittest discover -s tests`（当前 **316 项全绿**，2 skipped）+ `node scripts/shell_ui_smoke.js` + `python scripts/shell_regression.py`（真实 Key）；改数据结构前先备份 `extra-brain/`（含 WAL 边车），并在 QQ 侧重跑一次暗号验证确保不回退。

---

*文档维护：extra_brain 核心库维护团队  
最后更新：2026-11-07（补充 v0.6–v0.13 里程碑；当前版本 v0.13.4，测试 316 项）  
适用版本：extra_brain v0.1.0 → v0.13.4（当前）→ v0.14.0 方案 C（待启动）
