# 交接文档（Handover）

> 给接手本仓库的人：这份文档说明「现在做到哪了、怎么跑起来、有哪些坑、下一步干什么」。
> 最后更新：2026-11-01 午后（v0.6.0：情感状态机接通 + 文档整册对齐）。

---

## 1. 项目是什么

「额外大脑」（extra_brain）：给陪伴型 Agent 加一套**跨会话长期记忆**。核心理念不是「存下所有聊天」，而是**少量、高相关、可解释、可遗忘**，让模型在任意新上下文里仍能维持连续的身份、关系与「被记住的感觉」。**当前 v0.6.0**。

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
| Python 记忆库 | ✅ **v0.6.0**：四类记忆 + 双时态版本链 + 人格锚 + 情感五态（状态机已接通抽取→巩固→检索，含反漠视规则）+ 感觉/工作记忆 + 七因子保留度六档处置 + 四路召回 + 只读模式 + 存储统计 + 陪伴层（关系/图式/叙事/联想/主动）+ HNSW + 静态加密 + P0/P1 遗忘审批。**77 项单测全绿**（3.11/3.12 双版本 CI），真实 DeepSeek + 三天闭环联测通过 |
| sidecar（HTTP 服务） | ✅ 已落地并扩展：`/stats`、`/forget`、`--read-only`、加密口令透传 |
| CI | ✅ GitHub Actions（`.github/workflows/ci.yml`）：3.11/3.12 矩阵 + 77 项单测 + CLI 冒烟 |
| **AstrBot 适配器** | ✅ **已端到端验证**：注入、巩固、peer 跨会话共享、`/memory` 指令、暗号验证通过（2026-10-31） |
| **QQ 通道（NapCat + OneBot v11）** | ✅ **已端到端验证**：私聊按 QQ 号隔离记忆、`/reset` 后跨上下文答出暗号（2026-11-01） |
| **v0.5.0 升级回归** | ✅ 2026-11-01：先备份 → 重启 → 旧记忆零丢失（QQ 15 条 + WebChat 1 条）→ 新暗号「金枪鱼-8866」四步验证通过；七因子分档在真实数据生效（高激活 P5 / 新写入 P4） |
| 挂载规范 `docs/MOUNT.md` | ✅ 已沉淀（两挂载面 + 四契约 + 接入清单） |
| DSH 适配器 | ⛔ 插件模块加载失败（根因已定位见坑 4，**已被 AstrBot 路线取代，暂搁置**） |
| 定时/消息阈值自动巩固 | ✅ 阈值触发已实现（AstrBot 适配器内）；主动开口决策 API 已就绪（`should_proactive_open`），宿主定时器接线 🔜 |

**一句话**：记忆库 v0.6.0（路线规划特性全部落地 + 情感状态机接通、77 测试、CI）→ AstrBot → QQ 全链路跑通；机器人 QQ 号 `1178469499` 常驻本机；旧数据在新代码下零迁移兼容。

---

## 3. 关键文件

```
small_fat_fish/
├── docs/
│   ├── 长期记忆「额外大脑」架构规范 v0.9.md  # 设计规范 + 第10节实现对照
│   ├── README.md                          # Python 库接入与运行说明（v0.5.0）
│   ├── MOUNT.md                           # ★ 挂载规范：任何宿主接入 extra_brain 的通用契约
│   ├── MEMORY_ITERATION_PLAN.md           # ★ 迭代路线图（v0.2→v0.5 已全部落地）
│   ├── CHANGELOG.md                       # 变更记录
│   └── HANDOVER.md                        # 本文档
├── .github/workflows/ci.yml              # CI：3.11/3.12 + 77 测试 + CLI 冒烟
├── pyproject.toml                        # 包定义（无第三方依赖，version 0.5.0）
├── src/extra_brain/                      # ★ 核心库（宿主无关，打磨对象）
│   ├── models.py config.py embeddings.py extraction.py
│   ├── storage.py consolidation.py retrieval.py
│   ├── session.py engine.py server.py cli.py
├── scripts/                              # start-all / stop-all / status-all + demo_closed_loop
├── astrbot-plugin-extra-brain/           # ★ AstrBot 适配器（当前主力挂载）
│   ├── main.py                           # 薄适配器：双钩子 + /memory 指令 + local/http 双后端
│   ├── _conf_schema.json                 # WebUI 配置面板 schema
│   └── README.md
├── dsh-plugin-extra-brain/               # DSH 适配器（暂搁置，加载坑见 §6 坑 4）
└── tests/                                # 77 项测试（test_extra_brain 24 + test_server 8 + test_v2_features 45）

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
- **API Key**：DeepSeek 密钥**只通过环境变量 `DEEPSEEK_API_KEY` 使用，绝不写进任何文件、绝不被提交**。AstrBot 对话 provider 的 key 存于其 `data/cmd_config.json`（该文件在运行目录，不在仓库内）。
- **端口**：AstrBot dashboard `127.0.0.1:6185`（WebUI + 内置 ChatUI）；NapCat 反向 WS 连 AstrBot 的 `127.0.0.1:6199/ws`；NapCat WebUI `127.0.0.1:6099`（token 见启动日志）；sidecar 若用 http 后端仍是 `127.0.0.1:8756`。

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

所有脚本均在 `scripts/` 目录，共 3 个（start-all / stop-all / status-all），带端口占用检查与幂等保护（重复运行会提示已运行而不重复启动）。

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

排障辅助：`/memory status`（peer/后端/待巩固数）、`/memory list semantic`、`/memory core`、记忆库文件在 `<runtime>\data\extra_brain\`（如 `qq_FriendMessage_<QQ号>-<hash>.db`，私聊按 QQ 号、群聊按群号隔离）。

选「蓝鲸-7749」这种**不可猜**的暗号是关键，能排除「模型靠上文/常识蒙对」。**必须跨上下文**——同一会话里 transcript 自带上下文，会假阳性。

---

## 6. 必须知道的坑（接手先读）

0. ⚠️ **记忆库数据库初始化（超级重要，必须亲自确认）**：`data/extra_brain/` 目录下的 2 个 `.db` 文件（私聊按 QQ 号、WebChatUI 按登录用户隔离）只能在**最开始的时候初始化一次**。之后**不能随意删除或重新初始化**——这会导致该 peer 的全部长期记忆（Core、语义、情节、风格记忆 + 向量 + 审计日志）永久丢失。如果真需要重新初始化，必须**先征得本人（用户）亲自确认**，切勿自行决定。这是记忆库作为「产品」的核心资产，数据即资产，切勿误删。

1. **peer_id 不能用会话 ID**。AstrBot 的 webchat umo 是 `webchat:FriendMessage:webchat!<user>!<conv_id>`，**conv_id 每开一个新对话就变**——必须剥掉（适配器 `resolve_peer_id` 已处理，2026-10-31 首版正则没对上三段式 umo 导致每会话一个库，跨会话验证当场暴露）。QQ 侧天然正确：`aiocqhttp:FriendMessage:<QQ号>` / `GroupMessage:<群号>` 本身稳定。DSH 侧等价坑：`session.id` 每「新会话」一个新 peer。
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

---

## 7. 下一步（建议顺序）

> v0.2→v0.5 的路线（`MEMORY_ITERATION_PLAN.md`）已全部落地；情感状态机与 /memory stats/forget 已接通（2026-11-01）。以下是剩余收尾与下一期候选：

1. ~~**emotion_state 接进抽取链路**~~ ✅ 已落地：抽取 prompt 输出 `emotion_state`（新事件默认 open）与 `emotion_updates`（推进已有情节）；巩固侧状态机校验迁移（非法转换拒绝并记警告）；反漠视规则生效——未平复事件（open/acknowledged/attended 且带情绪）保底温层、永不自动归档、检索加权 +0.10 并豁免分数门槛。77 项单测覆盖。
2. ~~**`/memory status` 展示保留度档位分布**~~ ✅ 已落地：`/memory stats`（档位分布/归档比例/库体积）；`/memory forget <id>`（P1 墓碑默认；P0 物理删除需 `--hard --yes` 双确认，适配器不会自动批准）。
3. **主动开口定时器接线** 🔜：`should_proactive_open` 决策 API 已就绪，需要在 AstrBot 适配器注册 cron（AstrBot 有定时体系），拿「由头」交给模型生成开场白。
4. **工作记忆启动效应** 🔜：`working_set` 内实体下一轮检索加权（v0.2.1 遗留）。
5. **情感记忆保护 flags.bit6 / 多角度视角** 🔜：\|效价\|≥0.45 或唤起≥0.65 的记忆跳过情绪衰减、最多降到温层；fact/feeling/bond/story 四视角打标（v0.4.0 遗留）。
6. **群聊场景实测**：把机器人拉进群（peer 已按群号隔离；群聊需 @机器人 或 `/` 前缀唤醒）；观察群聊多人共享记忆库的行为是否符合预期。
7. **每日定时巩固窗口**（规范 §5.1 第三触发）：适配器注册每日任务调 consolidate。
8. **持久化运行**：AstrBot/NapCat 都挂在后台 job 里，重启机器后需手动按 §4 拉起；可做成计划任务/NSSM 服务。
9. 可选：真实 tokenizer 预算、`http` 后端多宿主共享部署、embedding 服务地址配置进 AstrBot 面板。
10. 搁置：DSH 插件加载修复（等有需要再按坑 4 方案修）。

---

## 8. 交接要点速查

- **跑测试**：`python -m unittest discover -s tests -v`（应 77/77 OK，3.11/3.12 均可）
- **启动全部**：`$env:DEEPSEEK_API_KEY="<key>"; .\scripts\start-all.ps1 [QQ号]`（工作目录：仓库根；AstrBot 运行目录固定为 `D:\projects_software\astrbot-runtime`）
- **停止全部**：`.\scripts\stop-all.ps1`
- **查看状态**：`.\scripts\status-all.ps1`
- **验证记忆**：暗号三步法见 §5（WebChatUI 与 QQ 两套均已验证）
- **AstrBot dashboard**：`http://127.0.0.1:6185`（密码见启动横幅/环境变量方式见 §4）
- **机器人 QQ**：`1178469499`（NapCat WebUI：`http://127.0.0.1:6099/webui`，token 见 NapCat 启动日志）
- **记忆库数据**：`D:\projects_software\astrbot-runtime\data\extra_brain\`（每 peer 一个 .db；换宿主=拷贝此目录）
- **本地离线体验**（不接宿主）：`extra-brain --db memory.db chat --threshold 8`
- **挂载新宿主**：读 `docs/MOUNT.md`，约 1 小时
