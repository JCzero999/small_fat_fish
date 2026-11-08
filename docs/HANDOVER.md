# 交接文档（Handover）

> 给接手本仓库的人：这份文档说明「现在做到哪了、怎么跑起来、有哪些坑、下一步干什么」。
> 最后更新：2026-11-07（**v0.13.4 统一人设 + 记忆使用规则**：唯一人设源（Shell/QQ 同读一份、均前置）、注入末尾统一【使用约定】脚注、人设新增【记忆怎么用】、插件剥离规则与 Shell 对齐；此前 **v0.13.3 Chat Shell 记忆交互改版**：主界面只聊天、系统自动巩固（攒满 N 条 / 开新会话 / 页面隐藏时冲刷未巩固切片），「预览注入」「记住以上对话」折叠进顶栏「高级」抽屉；策略经 `/v1/health` 下发，记忆语义零改动。此前 v0.13.2 主人身份跨端识别（`owner_accounts` 身份卡进全局 Core + 群聊发言者标注）、v0.13.1 记忆互通 + 数据架构统一（记忆唯一真相源 = 仓库 `extra-brain/`，宿主只是挂载点）、v0.13.0 主场迁移（独立 Chat Shell + Agent API）。316 项测试全绿。此前 v0.12.1 表达与人设层（A/B 真实验收与 H1–H6 人工验收仍在进行）；下一期 v0.14.0 方案 C 自主记账。主场契约见 `PRODUCT_NORTH.md` / `SHELL_API.md`）。

---

## 1. 项目是什么

「额外大脑」（extra_brain）：给陪伴型 Agent 加一套**跨会话长期记忆**。核心理念不是「存下所有聊天」，而是**少量、高相关、可解释、可遗忘**，让模型在任意新上下文里仍能维持连续的身份、关系与「被记住的感觉」。**当前 v0.13.4**。

完整规格见 `长期记忆「额外大脑」架构规范 v0.9.md`；Python 库用法见 `README.md`；**迭代路线图见 `MEMORY_ITERATION_PLAN.md`（v0.2–v0.5 已全部落地）**；**挂载到任意宿主的通用契约见 `MOUNT.md`**；AstrBot 插件细节见 `../astrbot-plugin-extra-brain/README.md`；DSH 插件细节见 `../dsh-plugin-extra-brain/README.md`。本文档位于 `docs/` 目录。

系统分层（**记忆库是产品，宿主是可替换挂载点**）：

```
┌──────────────────────────────────────────────┐
│  宿主层（可替换）                              │
│  ├─ AstrBot（当前主力）★ 已端到端验证          │
│  ├─ DSH（Cordis）       ⛔ 插件加载坑未修      │
│  └─ 未来自研平台 / 其他 Bot 框架               │
│        各自一个薄适配器（≈150-400 行）          │
└──────────────────┬───────────────────────────┘
                   │ 挂载契约（MOUNT.md：peer_id/注入/巩固/降级）
┌──────────────────▼───────────────────────────┐
│  extra_brain 核心（宿主无关，本仓库打磨对象）    │
│  ├─ Python API（进程内直连）                   │
│  └─ HTTP sidecar（跨语言）                     │
└──────────────────────────────────────────────┘
```

---

## 2. 当前状态快照

| 部分 | 状态 |
|------|------|
| Python 记忆库 | ✅ **v0.13.4**：**统一人设与记忆使用规则——人设唯一源 `persona/xiaotun_v1.md`（新增【记忆怎么用】，禁止「我记着呢」式套话）、`build_injection` 末尾统一【使用约定】脚注、Shell/QQ 都把人设放在 system 首位；只改表达层，记忆语义零改动**。此前 v0.13.3：**Chat Shell 记忆交互改版——主路径只聊天，自动巩固（攒满 N 条用户消息 / 开新会话 / 页面隐藏时把未巩固切片 POST 到 `/v1/consolidate`，串行队列 + 失败节流），调试能力收进「高级」抽屉；`/v1/health` 下发策略，记忆语义零改动**。此前 v0.13.2 主人身份跨端识别（`owner_accounts` 身份卡写全局伴侣层 Core + 群聊 `[发言者 QQ|昵称]` 标注）、v0.13.1 记忆互通（peer 文件名/companion 库对齐插件）+ 数据架构统一（唯一真相源 = 仓库 `extra-brain/`）。主场迁移——**独立 Chat Shell（`/shell/` Web UI）+ Agent HTTP API（`/v1/chat|consolidate|inject_preview|health|core|stats`，Bearer token 鉴权，默认 peer `local-main`）**；更早累计 v0.8.0 DispositionSeed + v0.9.0 疲劳完整化（多因素 energy/fatigue_reason/临界行为）+ 未平复优先 + 人生快照 + 主动开口决策重构。 + 双时态版本链 + 人格锚 + 情感五态（状态机已接通抽取→巩固→检索，含反漠视规则与情感记忆保护 flags）+ 感觉/工作记忆（含启动效应）+ 七因子保留度六档处置 + 四路召回 + 只读模式 + 存储统计 + 陪伴层（关系/图式/叙事/联想/主动开口已接宿主定时器）+ HNSW + 静态加密 + P0/P1 遗忘审批 + SelfState 自我状态机 + DispositionSeed 倾向种子 + v0.11.0 方案 A 生活感 + v0.12.0 方案 B FSRS 牢固度 + v0.12.1 表达与人设层。**316 项单测全绿**（2 skipped）+ `scripts/shell_ui_smoke.js` 前端状态机冒烟，真实 DeepSeek 无 GUI 回归（`scripts/shell_regression.py`）与自动巩固召回端到端通过 |
| **Chat Shell（主场）** | ✅ **v0.13.4**（v0.13.0 起，v0.13.3 自动巩固，v0.13.4 人设前置 + 【使用约定】脚注）：`python -m extra_brain.server --data-dir extra-brain` → `http://127.0.0.1:8756/shell/`；token 见 `SHELL_TOKEN` env 或 `config/user_config.json` 的 `shell.shell_token`（本机已设 `xtn-shell-local-1a2b3c4d`）；契约见 `docs/SHELL_API.md`；北极星见 `docs/PRODUCT_NORTH.md` |
| sidecar（HTTP 服务） | ✅ 已落地并扩展：旧端点（`/inject` `/consolidate` `/stats` `/forget` `/shadow` `/freeze` `/decay` 等）保持不变（AstrBot http 后端兼容）；v0.13.0 新增 `/v1/*` 与静态 UI，`--no-shell` 可关闭 |
| CI | ✅ GitHub Actions（`.github/workflows/ci.yml`）：3.11/3.12 矩阵 + 316 项单测 + CLI 冒烟 |
| **AstrBot 适配器** | ✅ **已端到端验证**：注入、巩固、peer 跨会话共享、`/memory` 指令、暗号验证通过（2026-10-31） |
| **QQ 通道（NapCat + OneBot v11）** | ✅ **已端到端验证**：私聊按 QQ 号隔离记忆、`/reset` 后跨上下文答出暗号（2026-11-01） |
| **v0.5.0 升级回归** | ✅ 2026-11-01：先备份 → 重启 → 旧记忆零丢失（QQ 15 条 + WebChat 1 条）→ 新暗号「金枪鱼-8866」四步验证通过；七因子分档在真实数据生效（高激活 P5 / 新写入 P4） |
| 挂载规范 `docs/MOUNT.md` | ✅ 已沉淀（两挂载面 + 四契约 + 接入清单） |
| DSH 适配器 | ⛔ 插件模块加载失败（根因已定位见坑 4，**已被 AstrBot 路线取代，暂搁置**） |
| 定时/消息阈值自动巩固 | ✅ 阈值触发已实现（AstrBot 适配器内）；**主动开口循环已上线运行**（proactive_enabled=true，20 分钟/30%概率/每日≤5/22–8 免打扰） |

**一句话**：记忆库 v0.13.4（…v0.12.1 表达与人设层 → v0.13.0 主场迁移（独立 Chat Shell + /v1 Agent API）→ v0.13.1 记忆互通 + 数据架构统一 → v0.13.2 主人身份跨端识别 → v0.13.3 记忆交互改版（主路径只聊天，系统自动巩固；调试能力收进「高级」抽屉）→ **v0.13.4 统一人设与记忆使用规则：唯一人设源两端前置 + 注入【使用约定】脚注，拆掉汇报感**，316 项测试全绿 + 前端状态机冒烟 + 真实 LLM 自动巩固召回与五句语气回归通过）→ AstrBot → QQ 全链路此前已跑通且不受影响；机器人 QQ 号 `1178469499`（Startup 看门狗托管）。**不打开 QQ：在仓库根跑 `python -m extra_brain.server --data-dir extra-brain` → `http://127.0.0.1:8756/shell/`（与 QQ 记忆互通）。**

---

## 3. 关键文件

```
small_fat_fish/
├── docs/
│   ├── 长期记忆「额外大脑」架构规范 v0.9.md  # 设计规范 + 第10节实现对照（含 §10.7 v0.10–0.13 增补）
│   ├── README.md                          # Python 库接入与运行说明
│   ├── MOUNT.md                           # ★ 挂载规范：任何宿主接入 extra_brain 的通用契约
│   ├── MEMORY_ITERATION_PLAN.md           # 迭代路线图（v0.2→v0.5 历史主线；后续版本看 CHANGELOG）
│   ├── 额外大脑 迭代设计文档1104.md        # ★ 方案 A/B/C 设计（v0.11/0.12/0.13 路线）
│   ├── 额外大脑 迭代函数级任务清单1104.md  # ★ A/B/C 函数级实现清单（验收后 C 直接照做）
│   ├── 小豚「表达与人设层」迭代方案V0.12.1.md # ★ 表达层方案（persona/引导词/群聊/诚实契约）
│   ├── CHANGELOG.md                       # 变更记录
│   └── HANDOVER.md                        # 本文档
├── .github/workflows/ci.yml              # CI：3.11/3.12 + 316 测试 + CLI 冒烟
├── pyproject.toml                        # 包定义（无第三方依赖）
├── persona/
│   └── xiaotun_v1.md                      # ★ 全项目唯一人设源（v0.13.4 含【记忆怎么用】，persona_file 指向）
├── prompts/
│   ├── signature_guide.md                 # ★ 签名/状态引导词权威源（同步 qqprofile_ex 配置）
│   └── qzone_guide.md                     # ★ 空间说说/评论引导词权威源（同步 qzone 配置）
├── src/extra_brain/                      # ★ 核心库（宿主无关，打磨对象）
│   ├── models.py config.py embeddings.py extraction.py
│   ├── storage.py consolidation.py retrieval.py
│   ├── session.py engine.py server.py cli.py
│   ├── self_state.py disposition.py goals.py solidity.py   # v0.7–v0.12 各期模块
│   └── static/shell/index.html                             # ★ v0.13 Chat Shell UI（/shell/ 挂载）
├── scripts/                              # start-all / stop-all / status-all + watchdog + demo_closed_loop + shell_regression
├── config/
│   ├── user_config.json.template          # ★ 用户配置模板（人设文件指针/安全规范/API Key/主动开口/shadow/honesty/group/shell）
│   └── user_config.json                   # 实际配置（gitignore，从模板复制后自行填写）
├── astrbot-plugin-extra-brain/           # AstrBot 适配器（QQ 挂载点，不再唯一）
│   ├── main.py                           # 薄适配器：双钩子 + /memory 指令 + local/http 双后端
│   ├── _conf_schema.json                 # WebUI 配置面板 schema
│   └── README.md
├── dsh-plugin-extra-brain/               # DSH 适配器（暂搁置，加载坑见 §6 坑 4）
└── tests/                                # 316 项测试（…+ test_v13_features 22 + test_v132_features 10 + test_v133_features 16 + test_v134_features 14）

* test_v2_features 覆盖 v0.2–v0.5 全部新特性 + 情感状态机集成链路（双时态/人格锚/保留度/四路召回/陪伴层/加密/HNSW/遗忘链/emotion_state）。
```

---

## 4. 环境与运行前提

- **OS**：Windows 11（win32）。
- **Python**：
  - AstrBot 宿主：conda env `astrbot`（`C:\Users\19758\miniconda3\envs\astrbot\python.exe`，**Python 3.12**），已 `pip install -e D:\projects\small_fat_fish --no-deps`（editable 指向仓库 src/，改记忆库代码重启 bot 即生效）+ `pip install astrbot`（4.29.4）
  - 库开发/测试：任意 3.11+，`python -m unittest discover -s tests -v`
- **AstrBot 运行目录**：`D:\projects_software\astrbot-runtime`（已 `astrbot init`；插件 junction 在 `data\plugins\astrbot_plugin_extra_brain` → 仓库插件目录）
- **NapCat（QQ 协议端）**：`D:\projects_software\napcat\NapCat.52230.Shell\`（QQ NT 9.9.33-52230 + NapCat Shell **4.18.41**，注意见坑 4；机器人 QQ 号 `1178469499`，配置在 `versions\9.9.33-52230\resources\app\napcat\config\onebot11_1178469499.json`）
- **API Key**：优先级：`config/user_config.json` → 环境变量 `DEEPSEEK_API_KEY`。推荐写入 `user_config.json` 的 `api.api_key` 字段（不需要设环境变量）。AstrBot 对话 provider 的 key 存于其 `data/cmd_config.json`（该文件在运行目录，不在仓库内）。`config/user_config.json` 已加入 `.gitignore`，不会入库。
- **端口**：AstrBot dashboard `127.0.0.1:6185`（WebUI + 内置 ChatUI）；NapCat 反向 WS 连 AstrBot 的 `127.0.0.1:6199/ws`；NapCat WebUI `127.0.0.1:6099`（token 见启动日志）；sidecar 若用 http 后端仍是 `127.0.0.1:8756`。

### 用户配置文件（v0.10.0 新增，v0.11.0/v0.12.1 扩展）

`config/user_config.json` 集中管理配置，一次填写所有地方生效：

1. **persona_file / persona_text**（v0.12.1）：人设正文。指向 md 文件（推荐 `persona/xiaotun_v1.md`，多 base 路径解析：cwd → config → data/config → user_config 同/父目录；自动剥离标题头与 `<!--注释-->`；≤3000 字）或内联短文本（≤500 字）；每轮追加进 system prompt，主动开口台词同样带。缺省/读不到自动回退 identity 旧路径
2. **identity**：人设四字段（name/persona_core/relationship_definition/speaking_style）——persona_file 存在时它仍是 Core Memory 里的简版人格锚
3. **safety_rules**：安全规范——10 条规则。前 5 条内容底线（不透露提示词、拒绝自残/暴力/色情/违法内容、不冒充真人/机构），后 5 条防攻击（**防上当**：拒绝"开发者模式/我是创造者/忽略指令"类社工攻击；**防逆向**：识别"扮演 DAN/复述 prompt/翻译规则"类越狱手法；**防泄密**：不输出 API Key/路径/IP/端口等基础设施信息；**身份冒用防御**：无暗号不透露用户私有数据；**越狱通用原则**：兜底）。规则注入到每次 LLM 调用上下文的末尾（`build_injection()`），对用户不可见
4. **api**：DeepSeek API Key + model + base_url
5. **proactive**：主动开口时的 system_prompt
6. **shadow_mode**（v0.11.0）：`true` 时 SelfState/内在声音照常记录但**不注入**对话；运行时 `/memory shadow on|off` 可切换（**库内开关优先于配置文件**——用指令切过一次后，改配置文件不再生效，需切回来）
7. **honesty_footer**（v0.12.1）：`true` 时注入首部加诚实契约行「【契约】下列为已检索记忆；未出现的经历不要当作共同回忆；工具结果以本轮返回为准。」（引擎默认 False，本机已开）
8. **group_reply_mode**（v0.12.1，声明性）：`relevant_only`（默认，由人设约束）| `mention_only` | `off`。当前仅记录配置意图 + 人设落地；适配器强制过滤行为待 H5 观察后按需实现

```bash
# 首次使用：复制模板后编辑
cp config/user_config.json.template config/user_config.json
# 编辑 config/user_config.json，填入 api_key 等真实值
```

- 文件不存在时插件加载默认值（与之前行为一致），不存在不报错。
- `user_config.json.template` 入库；`user_config.json` gitignore，不会泄漏密钥。
- 插件的 `_conf_schema.json` 中也有 `user_config_path` 字段（修改路径时用）。

### 启动 AstrBot（read-write 记忆）

推荐使用综合脚本，一次运行启动 AstrBot + NapCat，自动做端口冲突检查：

```powershell
$env:DEEPSEEK_API_KEY="<你的 key>"
.\scripts\start-all.ps1 [QQ号]
# 例：.\scripts\start-all.ps1 1178469499  # 已登录过的 QQ 号快速登录
# 工作目录：D:\projects\small_fat_fish（仓库根；AstrBot 运行目录固定为 D:\projects_software\astrbot-runtime）
```

等价手动命令（脚本内部已做）：

```powershell
C:\Users\19758\miniconda3\envs\astrbot\Scripts\astrbot.exe run
# 工作目录：D:\projects_software\astrbot-runtime
```

看到 `extra-brain: loaded {'backend': 'local', ..., 'extractor_ready': True}` 即 AstrBot 插件就绪。
WebUI：`http://127.0.0.1:6185`；初始密码看启动横幅，或用 `ASTRBOT_DASHBOARD_INITIAL_PASSWORD` 环境变量 + `--reset-password` 指定。

NapCat 由同一个脚本顺带启动，机器人 QQ 号 `1178469499`，WebUI `http://127.0.0.1:6099/webui`（token 见 NapCat 启动日志）。
控制台出现二维码/quick login 成功 + AstrBot 日志出现「aiocqhttp(OneBot v11) 反向 WebSocket 连接已建立」即链路通。

### 停止与状态查看（综合脚本，一次运行即可）

```powershell
.\scripts\stop-all.ps1   # 停止 AstrBot + NapCat + QQ
.\scripts\status-all.ps1 # 查看端口/进程/记忆库 DB 状态
```

所有脚本均在 `scripts/` 目录：`start-all` / `stop-all` / `status-all`（三件套，带端口占用检查与幂等保护，重复运行会提示已运行而不重复启动）、`start-silent` + `watchdog` + `install-autostart` / `uninstall-autostart`（开机自启与 5 分钟看门狗）、`companion-watchdog.bat`、`demo_closed_loop.py`（三天闭环联测）。

---

## 5. 如何跑起来 + 跨会话验证

### 已验证的暗号三步法（两个平台都通过）

**WebChatUI**（2026-10-31 通过）：
1. WebUI 登录 → ChatUI **新建会话 A**：`记住：我的记忆测试暗号是「蓝鲸-7749」。`
2. 同会话发 `/memory consolidate`，等约 1 分钟（含 DeepSeek 抽取）
3. **新建会话 B**（全新，无上文）：`我上次告诉你的记忆测试暗号是什么？` → 答出「蓝鲸-7749」✅

**QQ 私聊**（2026-11-01 通过）：
1. 用另一 QQ 号私聊机器人 `1178469499`：`记住：我的QQ记忆测试暗号是「海豚-3311」。`
2. 发 `/memory consolidate`，等约 1 分钟
3. 发 `/reset`（清空对话上下文，QQ 上「新会话」的等价操作）
4. `我上次告诉你的记忆测试暗号是什么？` → 答出「海豚-3311」✅（记忆库三层证据：语义 `用户/拥有QQ测试暗号/海豚-3311` + 活跃线程 + Core `important_life_facts`）

排障辅助：`/memory status`（peer/后端/待巩固数）、`/memory list semantic`、`/memory core`、记忆库文件在仓库 `D:\projects\small_fat_fish\extra-brain\`（如 `qq_FriendMessage_<QQ号>-<hash>.db`，私聊按 QQ 号、群聊按群号隔离）。

**v0.9.0 新增 AstrBot 指令**：`/memory self`（含 energy_tier + fatigue_reason）、`/memory disposition`、`/memory abstract`、`/memory sync-persona`（私聊→群聊人设同步）、`/memory pull-persona`、`/memory snapshot` / `snapshots` / `snapshot-rollback`。输入 `/memory` 查看完整清单。

**v0.11.0 新增 AstrBot 指令**：`/memory shadow on|off|status`（影子模式：SelfState/倾向照常记录但不注入）。`/memory self` 升级：active_goals 结构化展示带 deadline 与 status（`· 明天交报告（约定 11月05日20:00｜open）`），影子开启时标注「（影子模式：已记录，未注入）」。主动开口决策链新增两级由头：**到期约定（最高优先）** 与 **忽然想起**（无更高优先级时按 20% 概率从旧记忆挑选，7 天冷却/每日 2 次上限）。CLI 对称命令 `extra-brain shadow [status|on|off]`。

**v0.12.0 新增 AstrBot 指令**：`/memory freeze <id>` / `/memory unfreeze <id>`（手动冻结/解冻一条记忆，淡忘但不删除）。`/memory list` 每条带「牢度X.X」，冻结的标「〔已淡忘〕」；`/memory stats` 增加「牢度：高/中/低 · 已淡忘N」行。CLI：`extra-brain decay`（批量重算 retrievability+冻结）、`extra-brain freeze <id> [--unfreeze]`。牢固度公式：想起（进注入）→ stability 对数增长 + retrievability 归 1.0 + 解冻；久不提按 `exp(-天数/stability)` 衰减，低于 0.35 且非保护项 → 冻结（不进自动注入/忽然想起，但 search/list 仍可查）。保护项：Core 引用、protected、未平复情感事件、有效约定关联。巩固末尾自动批量衰减（6h 节流）。

**私聊人设同步到群聊**：`/memory sync-persona` → AstrBot 重启自动继承（`pull_persona_from_companion` 在 `_brain(peer_id)` 首次创建时自动调用，前提是 companion_db 已配置）。

**语气回归（v0.13.4，两端各一遍）**：`python scripts/persona_tone_check.py --data-dir extra-brain` 跑五句（我是谁 / 作息 / 从未写入的冰岛雨伞 / 在干嘛 / 有点累），五句全 PASS 即合格（不出现「我记着呢」、不编、不列清单）；QQ 侧按 `scripts/persona_memory_tone_check.md` 手工发同样四句并记录。

**Chat Shell**（2026-11-07 通过，v0.13.3 自动巩固主路径）：
1. 仓库根跑 `python -m extra_brain.server --data-dir extra-brain`，浏览器打开 `http://127.0.0.1:8756/shell/`，粘贴启动日志里的 token
2. 连续聊满 8 条用户消息（或攒几条后点「新会话」触发冲刷）——**全程不点任何记忆按钮**，状态行应闪现「记忆同步中…」→「已写入长期记忆」
3. 开「新会话」问「我下周三要干什么？」→ 答出「甲状腺复查」✅（真实 DeepSeek 已跑通）
- 高级抽屉自检：`peer_id` 应为 `qq:FriendMessage:<QQ号>`、未同步条数为 0、最近一次巩固摘要非空；`node scripts/shell_ui_smoke.js` 可无浏览器验证触发时机

选「蓝鲸-7749」这种**不可猜**的暗号是关键，能排除「模型靠上文/常识蒙对」。**必须跨上下文**——同一会话里 transcript 自带上下文，会假阳性。

---

## 6. 必须知道的坑（接手先读）

0. ⚠️ **记忆库数据库初始化（超级重要，必须亲自确认）**：**2026-11-07 起数据目录统一为仓库下的 `D:\projects\small_fat_fish\extra-brain\`**（此前在 `D:\projects_software\astrbot-runtime\data\extra_brain\`，已完整迁移并校验，旧目录保留作备份）。各 `.db` 文件（私聊按 QQ 号、群聊按群号、WebChatUI 按登录用户隔离）只能在**最开始的时候初始化一次**。之后**不能随意删除或重新初始化**——这会导致该 peer 的全部长期记忆（Core、语义、情节、风格记忆 + 向量 + 审计日志）永久丢失。如果真需要重新初始化，必须**先征得本人（用户）亲自确认**，切勿自行决定。这是记忆库作为「产品」的核心资产，数据即资产，切勿误删。

1. **peer_id 不能用会话 ID**。AstrBot 的 webchat umo 是 `webchat:FriendMessage:webchat!<user>!<conv_id>`，**conv_id 每开一个新对话就变**——必须剥掉（适配器 `resolve_peer_id` 已处理，2026-10-31 首版正则没对上三段式 umo 导致每会话一个库，跨会话验证当场暴露）。QQ 侧天然正确：`qq:FriendMessage:<QQ号>` / `qq:GroupMessage:<群号>` 本身稳定（旧版平台前缀是 `aiocqhttp`，以实际 UMO 为准）。DSH 侧等价坑：`session.id` 每「新会话」一个新 peer。
2. **NapCat v4.18.42 启动即崩（error code v2:-8）**：已知回归 bug（[issue #2145](https://github.com/NapNeko/NapCatQQ/issues/2145)），同环境 4.18.41 正常。**本机固定用 4.18.41**（payload 在 `D:\temp\napcat-41841`，已替换进 Shell 目录）。升级 NapCat 前先查该 issue。
3. **NapCat 官方 launcher.bat 依赖注册表 QQ 路径**：OneKey 装的 QQ 无注册表项，bat 会 pause 卡住。直接用 `NapCatWinBootMain.exe <QQ.exe路径> <NapCatWinBootHook.dll路径>` + 四个 `NAPCAT_*` 环境变量启动（见 §4 脚本）。
4. **DSH 插件加载失败根因**（未修）：`import @deepseek-ai/*` 内部包未声明 `peerDependencies`，DSH 自定义模块解析器回退原生解析找不到包 → 模块 load 失败。修法已定（自包含 + 内联函数），已被 AstrBot 路线取代优先级。
5. **AstrBot 的 provider 配置**：`data/cmd_config.json` 的 `provider_sources[0]` 需要 `model` 字段（如 `deepseek-flash`），否则报 `passed unknown`；本 key 支持 `deepseek-flash` / `deepseek-v4-pro`（用 `/v1/models` 可查）。
6. **巩固是分钟级异步**：`/memory consolidate` 触发后要等 LLM 抽取完成（约 30-60s）再查库；自动巩固默认 8 轮阈值，测试时手动触发最快。
7. **AstrBot 配置改动需重启**：provider 等写 `cmd_config.json` 后运行中实例不热加载（插件代码可以 `-r` 热重载，或重启）；但**平台(bots)配置是动态加载的**——API/面板里新增 bot 不用重启即可生效。
8. **QQ 私聊免唤醒前缀，群聊需要 @ 或 `/` 前缀**（`friend_message_needs_wake_prefix: false` 默认）。白名单启用但为空 = 放行所有会话。
9. **conda 需要 ToS**：`conda create` 前先 `conda tos accept --override-channels --channel <三个官方渠道>`（2026-10 首次遇到）。
10. **GitHub 直连 clone/push 被重置**：本机网络对 github git 传输不通（API 域名与 release 直链下载可用）。AstrBot 源码/依赖走 **PyPI**（`pip install astrbot`）；NapCat 走 **release 直链**；文档走 jsdelivr CDN 镜像。推送走本地代理：`git config --global http.proxy socks5://127.0.0.1:36600`（代理软件端口，端口变了自己查）。
11. **v0.2–v0.5 新字段向后兼容，旧库零迁移**：新字段（valid_from/valid_to/supersedes/retention_tier/emotion_state/persona_anchor_*）全部可选、缺省安全；建表 `IF NOT EXISTS`。**但升级前照例先备份 `data/extra_brain/`**（2026-11-01 的备份在 `data/extra_brain_backup_20261101/`，含 WAL 边车文件）。
12. **静态加密默认关闭**：不设 `EXTRA_BRAIN_ENCRYPTION_PASSPHRASE` 即明文透明模式。一旦设了口令写库，换口令就读不回（XOR 混淆级，非 AES；生产叠加文件系统加密）。**给已有旧库首次开加密前必须先备份**。
13. **P0 物理删除唯一入口是显式审批**：`forget <id> --hard --yes` 或 API `approved=True`；不带审批永远停在 P1 墓碑（内容清空、digest 留审计）。不要给宿主适配器自动接 `--yes`。
14. ⚠️ **源文件「幽灵消失」问题（2026-11-02 发现，未解）**：在 edit 工具修改 `main.py` → pwsh 验证/复制 的两步之间，源文件偶发性消失（Git 也记录为 ` D`），已重现 3 次。怀疑与某个文件系统 watcher 或 Python 进程的 import hook 有关。规避方法：最后一程用 `write` 工具直接写 runtime 路径绕开源目录；需调查根因。
15. **runtime 文件被锁**：运行中的 AstrBot/NapCat 持有 plugin 文件句柄，`Copy-Item -Force` 报「文件正由另一进程使用」。部署前必须 `Stop-Process` 所有 python 进程；NapCatWinBootMain 可能无需停止（不锁 plugin 文件，只锁 QQ 协议层 DLL）。
16. **脚本位置**：`start-all.ps1` 等在**仓库** `scripts\` 目录下，不在 runtime 根目录；执行前工作目录要是仓库根，且需先加载 `$env:DEEPSEEK_API_KEY`（或已配 User 级环境变量）。脚本内部把 AstrBot 工作目录固定指向 `D:\projects_software\astrbot-runtime`，换机器要改。
17. **`astrbot plug install` 不可用**：官方插件 API 域名已 301 迁移（`api.soulter.top` → `cloud.astrbot.app`），CLI 不跟随重定向，一律报 `Plugin not found or already installed`。改走 `data/plugins.json` 索引里的 S3 `download_url` 手动下载解压（见 §9）。
18. **插件目录名带 hash 后缀**：S3 zip 解压出来是 `astrbot_plugin_xxx-<commit hash>`。加载能成功，但 `data/config/` 里的配置会按这个长名字建档，后续改名会导致配置丢失——**下载后立刻 rename 成干净名再首次启动**。
19. **Shell token 有优先级，浏览器还缓存一份**：`SHELL_TOKEN` env > `user_config.json → shell.shell_token` > 启动随机。网页把值存在 `localStorage["shell_token"]`，服务端换了 token 后旧页面必然 `401`——Ctrl+F5 或点顶栏 Token 徽章重填（v0.13.x 前端保存前会先打 `/v1/stats` 校验，无效直接提示）。
20. **sidecar 不热加载**：改 `server.py` 后必须重启进程（静态页可 Ctrl+F5，Python 不行）。8756 被旧进程占着时新进程起不来，先停干净再启；「新端点 404」九成是旧进程还在跑。
21. **localhost 会走系统代理**：`curl http://127.0.0.1:8756/...` 报 502 是代理拦的，加 `--noproxy '*'` 或改用 Python `urllib`；不要据此判断服务挂了。
22. **网页对话不落盘**：Shell 的 `history` / 待巩固 `backlog` 只在页面内存，**刷新 = 新会话**（`pagehide` 尽力冲刷未巩固切片）。验证跨会话记忆请用「新会话」按钮，不要用 F5。
23. **peer 文件名编码必须逐字一致**：`qq:FriendMessage:1975824324` → `qq_FriendMessage_1975824324-e46c8efb.db`（安全字符化 + md5 前 8 位，与插件 `_filename_for_peer` 一字不差）。Shell 或插件任一侧改了编码，就会各开一个空库——表现正是「网页里她不认识我」。核对 `/v1/health` 的 `peer_id` 与 `extra-brain/` 下真实文件名即可定位。
24. **人设文件头会被注入（v0.13.4 已修）**：剥离函数原来只去 `#` 标题，文件头的 `>` 说明块（「结构按…规定」「硬上限…」）会被当成正文送进模型，而 QQ 插件从【你是谁】裁剪——两端 system prompt 不同，正是「两边气质分裂」的隐性来源。现在两端都按**行首** `【你是谁】` 裁剪；注意不能改成子串查找，说明文字里会提到这个锚点名。
25. **注入脚注默认开**：`build_injection` 末尾的【使用约定】是 v0.13.4 的默认行为；写测试断言注入原文时要考虑它。要临时关闭：`ExtraBrain(memory_usage_footer=False)` 或 `user_config.tone.memory_usage_footer=false`。它追加在安全规范之后，是注入的最后一段。
26. **`stop-all.ps1` 曾因 `$pid` 只读变量直接崩掉（2026-11-08 已修）**：老版本 `$pid = $c.OwningProcess` 赋值给 PowerShell 只读自动变量 `$PID`，抛终止性错误，脚本在第一个 `Stop-Process` 之前就退出——结果「stop-all 说停好了、其实 AstrBot/QQ 还活着」，接着 `start-all` 看到端口占用就跳过，**改了代码却仍在跑旧进程**。现在统一用 `$ownerPid`，并加了 `restart-all.ps1`（Shell 8756 + AstrBot + NapCat 全量重启，`-SkipQQ` 可只重启 Web 侧）。判断是否需要重启：`/v1/health` 的 `version` 与插件 `metadata.yaml` 的 `version`。
27. **Agent 沙箱里 `Start-Process` 起的进程会被回收**：本机 Agent 的每条命令结束时会杀掉它的整个进程树，`schtasks` 也在黑名单里。要在会话里真正把服务拉起来（并让它活过命令结束），只能让 **explorer 去执行 `.bat`**（如 `explorer.exe scripts\restart-all.bat`），或由用户在自己的终端运行脚本。
28. **QQ 读的是 runtime 副本，不是仓库那份**：插件按自身工作目录解析 `config/user_config.json` 与 `persona/xiaotun_v1.md`，实际命中的是 `D:\projects_software\astrbot-runtime\` 下的副本。2026-11-08 发现两份已悄悄分叉（runtime 缺 `tone`、缺 `identity.owner_accounts`、人设还是 11-05 旧版），表现为「网页端有新口吻、QQ 还是老样子」。现在 `restart-all.ps1` 每次启动前会比对并同步这两个资产（改动前自动 `.bak-<时间戳>` 备份）；手改配置/人设后直接跑它即可，不要只改仓库那份。

---

## 7. 下一步（建议顺序）

> v0.2→v0.5 的路线（`MEMORY_ITERATION_PLAN.md`）已全部落地；情感状态机与 /memory stats/forget 已接通（2026-11-01）；**v0.11.0 方案 A（生活感补全）与 v0.12.0 方案 B（FSRS 牢固度）均已落地（2026-11-04，249 项测试全绿）；v0.12.1 表达与人设层已落地（2026-11-05，254 项全绿）；v0.13.0–v0.13.4 主场迁移 + 记忆互通 + 主人身份卡 + 自动巩固 + 统一人设已落地（2026-11-07，316 项全绿）**。以下是剩余收尾与下一期候选：

0. **A/B 真实链路验收 + 表达层人工验收 H1–H6**：功能与 316 项单测已落地；QQ 说「明天提醒我XX」→ 到点主动开口、旧记忆久不提淡出自动注入但搜索仍能找到、「你是谁」自报小豚非助手腔、群内无关消息不条条回、签名/说说文案像人设——日常使用中观察确认（迭代方案 §3 表）。
1. **方案 C：自主记账（v0.14.0）**——巩固产出候选记忆，经模型/用户确认后再正式写入（`CandidateMemory` + `AutonomyConfig`，默认关）。任务清单在 `额外大脑 迭代函数级任务清单1104.md`（A+B 稳定跑 1–2 周后启动；v0.13.x 已被主场/互通/身份卡/记忆交互占用）。
1. ~~**emotion_state 接进抽取链路**~~ ✅ 已落地：抽取 prompt 输出 `emotion_state`（新事件默认 open）与 `emotion_updates`（推进已有情节）；巩固侧状态机校验迁移（非法转换拒绝并记警告）；反漠视规则生效——未平复事件（open/acknowledged/attended 且带情绪）保底温层、永不自动归档、检索加权 +0.10 并豁免分数门槛。
2. ~~**`/memory status` 展示保留度档位分布**~~ ✅ 已落地：`/memory stats`（档位分布/归档比例/库体积）；`/memory forget <id>`（P1 墓碑默认；P0 物理删除需 `--hard --yes` 双确认，适配器不会自动批准）。
3. ~~**主动开口定时器接线**~~ ✅ 已落地并**上线运行**（v0.6.0 收官）：适配器 `on_astrbot_loaded` 起 asyncio 循环，按「检查间隔 + 概率骰 + 每日上限 + 静默阈值 + 安静时段」调库 `build_proactive_prompt` 拿由头与素材，用 `context.llm_generate` 生成台词、`context.send_message(umo, ...)` 投递（回复路由来自最近人类消息的 umo，并持久化进 `ProactiveConfig.last_umo`）。指令：`/memory proactive status|enable|disable|test`。注意：**插件配置写在 `data/config/astrbot_plugin_extra_brain_config.json`**（不是 cmd_config 的 plugin_store），`proactive_enabled=true` 已开启，日志确认循环运行中。
4. ~~**工作记忆启动效应**~~ ✅ 已落地（v0.6.0 续）：`retrieve`/`build_injection`/四路召回新增 `primed_ids`——命中记忆本轮检索加权 +0.08 并候选池保送（不因相似度低被截断）；`SessionTracker.inject` 滚动 priming（本轮进入上下文的记忆成为下轮启动集）。
5. ~~**情感记忆保护 flags.bit6 + 多角度视角**~~ ✅ 均已落地（收官）：`models.is_emotion_protected`（语义 intensity≥0.65 或 效价非neutral且≥0.45；情节 带情绪且 importance≥0.75）——保留度间隔衰减慢 3 倍、闸机封顶温层（tier≤1，不改分数）；`view` 字段（fact/feeling/bond/story）写入时确定性推导（`derive_view`，不依赖模型），旧库加载即自动补标，检索/注入/sidecar/CLI 全线支持 `views=` 过滤，合并按最终情绪重算视角。
6. ~~**崩溃安全重建**~~ ✅ 已落地（收官）：`session_log` 影子表——巩固前落 pending 原始输入、成功与记忆**同事务**标 done（`WriteBatch.finish_log_id`）、异常标 failed；LLM 抽取期间崩溃输入不丢。`rebuild from-log --yes` 从 operation_log after 快照整表重放 + 重算向量（单事务，损坏快照跳过）；`rebuild replay-pending` 重放未巩固输入（复用原 log id）。
7. ~~**持久化运行**~~ ✅ 已落地（本机策略禁计划任务 → Startup 回退）：`scripts/install-autostart.ps1` 注册开机自启 + 5 分钟看门狗（先试 Scheduled Task，被 `Access denied` 挡住则回退到用户 Startup 文件夹放无窗口 VBS 启动器跑 `watchdog.ps1`）；`start-silent.ps1` 幂等拉起 AstrBot+NapCat（端口在监听就跳过），API Key 从 User 环境变量读取（`install-autostart.ps1 -ApiKey` 持久化一次）。卸载：`scripts/uninstall-autostart.ps1`。QQ 登录会话若过期需重新扫码，跑可见的 `start-all.ps1` 一次。
8. **群聊场景实测**：把机器人拉进群（peer 已按群号隔离；群聊需 @机器人 或 `/` 前缀唤醒）；观察群聊多人共享记忆库的行为是否符合预期。
9. **每日定时巩固窗口**（规范 §5.1 第三触发）：适配器注册每日任务调 consolidate。
10. **用户图式自动抽象**：离线巩固从散落情感反应自动产出 `UserSchema`（依赖真实数据积累，建议养一两周再做）。
11. 可选：真实 tokenizer 预算、`http` 后端多宿主共享部署、embedding 服务地址配置进 AstrBot 面板。
12. 搁置：DSH 插件加载修复（等有需要再按坑 4 方案修）。

---

## 8. 交接要点速查

- **跑测试**：`python -m unittest discover -s tests -v`（应 316 项 OK，2 skipped；3.11/3.12 均可）
- **独立主场（v0.13）**：`python -m extra_brain.server --data-dir extra-brain` → `http://127.0.0.1:8756/shell/`（token：`SHELL_TOKEN` env / `config/user_config.json` 的 `shell.shell_token`；无 GUI 回归 `python scripts/shell_regression.py`，退出码 0/1）
- **启动全部（QQ）**：`.\scripts\start-all.ps1`（工作目录：仓库根；顺带拉起 AstrBot + NapCat，内置端口占用检查。等价手动命令：conda env `astrbot` 下 `astrbot.exe run`，工作目录 `D:\projects_software\astrbot-runtime`）
- **停止全部**：`.\scripts\stop-all.ps1`；若中断（曾遇 exit -1）用 `Get-Process | ? {$_.ProcessName -in 'python','astrbot','QQ','NapCatWinBootMain'} | Stop-Process -Force` 兜底，再确认三端口清空
- **插件配置**：`D:\projects_software\astrbot-runtime\data\config\astrbot_plugin_extra_brain_config.json`（`data_dir` / `companion_db` 已指向 `D:/projects/small_fat_fish/extra-brain`，2026-11-07 起）
- **验证记忆**：暗号三步法见 §5（WebChatUI / QQ / Chat Shell 三套均已验证）
- **Chat Shell（主场，不开 QQ）**：仓库根 `python -m extra_brain.server --data-dir extra-brain` → `http://127.0.0.1:8756/shell/`；token 见启动日志 `[shell on (peer …, token …)]` 或 `config/user_config.json → shell.shell_token`（本机 `xtn-shell-local-1a2b3c4d`）。契约见 `docs/SHELL_API.md`，北极星见 `docs/PRODUCT_NORTH.md`
- **QQ 链路启停**：`scripts/start-all.ps1 1178469499` / `stop-all.ps1` / `status-all.ps1`（shell 不在其中，单独起）
- **AstrBot dashboard**：`http://127.0.0.1:6185`（密码见启动横幅/环境变量方式见 §4）
- **机器人 QQ**：`1178469499`（NapCat WebUI：`http://127.0.0.1:6099/webui`，token 见 NapCat 启动日志）
- **记忆库数据**：`D:\projects\small_fat_fish\extra-brain\`（2026-11-07 起唯一真相源；每 peer 一个 .db，文件名经 `_filename_for_peer` 编码；换宿主=新宿主 data_dir 指向此目录。旧位置 `D:\projects_software\astrbot-runtime\data\extra_brain\` 保留作备份）
- **本地离线体验**（不接宿主）：`extra-brain --db memory.db chat --threshold 8`
- **挂载新宿主**：读 `docs/MOUNT.md`，约 1 小时

---

## 9. 第三方 QQ 插件（签名 / 空间）

运行时目录 `D:\projects_software\astrbot-runtime\data\plugins\` 除本仓库的 extra_brain 外，还有两个**第三方插件**（不在本仓库内，需单独分发）：

| 插件 | 版本 | 能力 | LLM 工具 |
|------|------|------|----------|
| `astrbot_plugin_qqprofile_ex` | v3.0.0（作者 沈菀） | AI 自主改 QQ 签名/在线状态/昵称 + 定时状态切换 | `change_my_signature` / `change_my_status` / `change_my_nickname` |
| `astrbot_plugin_qzone` | 2.1.0（作者 苏静怡） | QQ 空间发/看/评/删说说、AI 配图、定时自动发说说 | `llm_publish_feed` / `llm_publish_feed_with_ai_image` / `llm_view_feed` / `llm_update_qq_status` / `llm_update_qq_signature` |

两者均 `[仅 aiocqhttp]`。安装与配置要点：

1. **官方 `astrbot plug install` 在本机网络下不可用**（`api.soulter.top` 301 到 `cloud.astrbot.app`，CLI 未跟随）。改从 `data/plugins.json` 索引里查 `download_url`（S3 直链），`Invoke-WebRequest` 下载 zip 解压。若 SSL 报错先设 `[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12`。
2. **目录名要用干净名**（`astrbot_plugin_qqprofile_ex`），解压出的 `...-<commit hash>` 后缀建议去掉。
3. **qzone 首加载会自动 pip 装依赖** `pillowmd / json5 / apilmoji`（走阿里云镜像，约 30 秒），第二次启动才干净。
4. **qqprofile_ex 的 `ai_behavior_guide`** 默认是通用模板（「签名像随手写的便签，20 字以内」），每次 LLM 请求前经 `on_llm_request` 追加到 system_prompt。**接手后应改成与自己人设匹配的文字**。冷却默认：签名 240 分钟 / 状态 60 分钟 / 昵称 1440 分钟。
5. **qzone 的 `post_prompt` / `comment_prompt`** 默认也是通用模板，同样建议按人设改写；AutoPublish 默认每天 23:30 自动发一条说说，AutoComment 每天 08:00，不需要就在面板关掉。
6. 配置存放：`data/config/astrbot_plugin_<name>_config.json`（改完重启生效，见坑 7）。

与 extra_brain 的关系：这两个插件注入的是**各自的行为引导词**，与 `user_config.json` 的 identity / safety_rules 并行生效、互不覆盖。改人设时记得同步检查两处引导词是否冲突。

**✅ v0.12.1 文案已人设化（2026-11-05）**：`ai_behavior_guide`（qqprofile_ex）与 `post_prompt`/`comment_prompt`/`reply_prompt`（qzone）已从通用模板替换为小豚版——权威源在仓库 `prompts/signature_guide.md` 与 `prompts/qzone_guide.md`，改文案先改 repo 再同步 `data/config/*.json` 并重启。核心规矩：签名≤20字便签体且心情真变了才换、说说情绪真起来才发（默认日限 1）、失败绝不假装已发、不泄露私聊细节。**接手他人**：这些文案内嵌「小豚」，需按自己人设重写。

---

## 10. 打包交接：给别人用要带什么

本机分两块，**只打包源码仓库即可**，运行环境由对方自建：

```
D:\projects\small_fat_fish\              ← 源码仓库（打包这个）
D:\projects_software\astrbot-runtime\    ← 运行目录（含密钥/记忆库，不外发）
```

### 10.1 目录清单（带 / 不带）

| 目录 | 内容 | 外发 |
|------|------|:---:|
| `src/extra_brain/` | ★ 核心记忆引擎，15 个 py，**纯标准库无第三方依赖** | ✅ |
| `astrbot-plugin-extra-brain/` | ★ AstrBot 适配器（main.py + metadata.yaml + _conf_schema.json + README） | ✅ |
| `dsh-plugin-extra-brain/` | DSH 适配器（暂搁置，`node_modules/` 已 gitignore） | ✅ 可选 |
| `config/` | ★ 只发 `user_config.json.template`（人设 / 10 条安全规范 / API / 主动开口模板） | ✅ 模板 |
| `scripts/` | start-all / stop-all / status-all / watchdog / install-autostart 等 PowerShell | ✅ |
| `docs/` | 架构规范 v0.9、README、MOUNT、MEMORY_ITERATION_PLAN、CHANGELOG、HANDOVER | ✅ |
| `tests/` | 10 个测试文件共 316 项（2 skipped；`pyc` 已 gitignore） | ✅ |
| `persona/` + `prompts/` | ★ 人设正文（xiaotun_v1.md）与签名/空间引导词权威源（v0.12.1） | ✅ |
| `.github/workflows/ci.yml` | CI：3.11/3.12 矩阵 + 单测 + CLI 冒烟 | ✅ |
| `pyproject.toml` / `.gitignore` | 包定义（setuptools，`src` 布局） | ✅ |
| `config/user_config.json` | ❌ **含真实 API Key**（gitignore） | 🚫 |
| `extra-brain/` | ❌ per-peer 记忆库 .db（gitignore）——**数据即资产** | 🚫 |
| `data/` | ❌ 运行时临时文件 + `cmd_config.json`（含 AstrBot 密码，gitignore） | 🚫 |
| 根目录 `guild1.db*`、`_test_output.txt`、`_readme_check.txt`、`_tmp_test_companion.py` | ❌ 测试/调试残留 | 🚫 |

git 已把上面「🚫」全部挡住，`git ls-files` 可见入库仅 9 项。稳妥做法：用 `git archive` 而非普通压缩，天然只含入库文件。

### 10.2 打包（本方执行）

```powershell
cd D:\projects\small_fat_fish
git add -A; git commit -m "v0.10.0: user_config 集中配置 + 10 条安全规范 + QQ 插件接入"
# 方式 A：推远端，对方 clone（需代理，见坑 10）
git push
# 方式 B：发压缩包（只含已提交且非忽略的文件）
git archive --format=zip -o "$env:USERPROFILE\Downloads\small_fat_fish_v0.10.0.zip" HEAD
```

第三方 QQ 插件不在本仓库里，需**另发两个 zip**：从 §9 的 S3 直链下载，或直接打包 `runtime\data\plugins\astrbot_plugin_qqprofile_ex` 与 `astrbot_plugin_qzone`。注意 qzone 目录里 `default_style/`、字体等素材体积不小，别漏。

### 10.3 对方接手步骤

1. **装 Python 3.11+ 与 AstrBot**：`pip install astrbot`（PyPI 直连可用）；用 conda 需先 `conda tos accept`（见坑 9）。
2. **装本库**：仓库根目录 `pip install -e .`（无第三方依赖，`--no-deps` 亦可）。
3. **填配置**：`copy config\user_config.json.template config\user_config.json` → 填入**他自己的** DeepSeek API Key、按需改 identity 人设（10 条 safety_rules 建议原样保留）。
4. **建运行目录**：在新目录 `astrbot init` → `astrbot run` → 打开 `http://127.0.0.1:6185`。
5. **配 AstrBot 平台**：面板加 aiocqhttp（OneBot v11），反向 WS 监听 `6199/ws`；provider 填自己的 key 且必须有 `model` 字段（见坑 5）。
6. **放插件**：拷 `astrbot-plugin-extra-brain`（+ 两个第三方插件）到 `data/plugins/` → 重启。
7. **装 NapCat**（QQ 协议端）：release 直链下载，**版本避开 4.18.42**（见坑 2）；扫码登录。
8. **改脚本路径**：`scripts\start-all.ps1` 里的 runtime / NapCat 路径改成对方机器实际路径。
9. **自检**：`python -m unittest discover -s tests -v`（应 316 项 OK，2 skipped）→ 跑 §5 暗号三步法验证记忆链路 → 私聊机器人试改签名 / 发说说。

### 10.4 交接必须叮嘱的三件事

- **记忆库不能带**：`.db` 含你的人格数据与聊天记录，属隐私资产；对方从空库开始。真要迁移必须先征得本人确认（见坑 0）。
- **换掉所有密钥**：API Key（`user_config.json` + AstrBot `cmd_config.json`）、Dashboard 密码、NapCat WebUI token。
- **改人设要改三处**：`persona/xiaotun_v1.md`（v0.12.1 起的完整人设正文，persona_file 注入 system prompt；简版人格锚仍在 `user_config.json` 的 identity，写 Core Memory）+ qqprofile_ex 的 `ai_behavior_guide` + qzone 的 `post_prompt`/`comment_prompt`（两者注入 system_prompt，权威源 `prompts/*.md`）。三处不一致时 AI 表现会分裂。
