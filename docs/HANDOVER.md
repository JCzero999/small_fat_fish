# 交接文档（Handover）

> 给接手本仓库的人：这份文档说明「现在做到哪了、怎么跑起来、有哪些坑、下一步干什么」。
> 最后更新：2026-11-01 凌晨（QQ 通道验证通过后）。

---

## 1. 项目是什么

「额外大脑」（extra_brain）：给陪伴型 Agent 加一套**跨会话长期记忆**。核心理念不是「存下所有聊天」，而是**少量、高相关、可解释、可遗忘**，让模型在任意新上下文里仍能维持连续的身份、关系与「被记住的感觉」。

完整规格见 `长期记忆「额外大脑」架构规范 v0.9.md`；Python 库用法见 `README.md`；**挂载到任意宿主的通用契约见 `MOUNT.md`**；AstrBot 插件细节见 `../astrbot-plugin-extra-brain/README.md`；DSH 插件细节见 `../dsh-plugin-extra-brain/README.md`。本文档位于 `docs/` 目录。

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
| Python 记忆库（四类记忆/抽取/检索/衰减/审计） | ✅ 已落地，30 项单测通过（Python 3.12 亦全绿），真实 DeepSeek + 三天闭环联测通过 |
| sidecar（HTTP 服务） | ✅ 已落地，桥接冒烟通过 |
| **AstrBot 适配器** | ✅ **已端到端验证**：注入、巩固、peer 跨会话共享、`/memory` 指令、暗号验证通过（2026-10-31） |
| **QQ 通道（NapCat + OneBot v11）** | ✅ **已端到端验证**：私聊按 QQ 号隔离记忆、`/reset` 后跨上下文答出暗号「海豚-3311」（2026-11-01） |
| 挂载规范 `docs/MOUNT.md` | ✅ 已沉淀（两挂载面 + 四契约 + 接入清单） |
| DSH 适配器 | ⛔ 插件模块加载失败（根因已定位见坑 4，**已被 AstrBot 路线取代，暂搁置**） |
| 定时/消息阈值自动巩固 | ✅ 阈值触发已实现（AstrBot 适配器内）；每日窗口 🔜 可选 |

**一句话**：记忆库 → AstrBot → QQ 全链路已跑通并验证；机器人 QQ 号 `1178469499` 常驻本机。下一步是日常使用中打磨记忆库本体。

---

## 3. 关键文件

```
small_fat_fish/
├── docs/
│   ├── 长期记忆「额外大脑」架构规范 v0.9.md  # 设计规范 + 第10节实现对照
│   ├── README.md                          # Python 库接入与运行说明
│   ├── MOUNT.md                           # ★ 挂载规范：任何宿主接入 extra_brain 的通用契约
│   ├── CHANGELOG.md                       # 变更记录
│   └── HANDOVER.md                        # 本文档
├── pyproject.toml                        # 包定义（无第三方依赖）
├── src/extra_brain/                      # ★ 核心库（宿主无关，打磨对象）
│   ├── models.py config.py embeddings.py extraction.py
│   ├── storage.py consolidation.py retrieval.py
│   ├── session.py engine.py server.py cli.py
├── scripts/demo_closed_loop.py           # 三天跨会话闭环演示
├── astrbot-plugin-extra-brain/           # ★ AstrBot 适配器（当前主力挂载）
│   ├── main.py                           # 薄适配器：双钩子 + /memory 指令 + local/http 双后端
│   ├── _conf_schema.json                 # WebUI 配置面板 schema
│   └── README.md
├── dsh-plugin-extra-brain/               # DSH 适配器（暂搁置，加载坑见 §6 坑 3）
└── tests/                                # 30 项测试
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
10. **GitHub 直连 clone 被重置**：本机网络对 github git 传输不通（API 域名与 release 直链下载可用）。AstrBot 源码/依赖走 **PyPI**（`pip install astrbot`）；NapCat 走 **release 直链**；文档走 jsdelivr CDN 镜像。

---

## 7. 下一步（建议顺序）

1. **日常使用 + 打磨记忆库本体**（当前重心）：QQ/ChatUI 真实使用中观察抽取质量/检索相关性/改口冲突，库代码改动 → 重启 bot 即生效（editable 安装直连仓库 src/）。
2. **群聊场景**：把机器人拉进群实测（peer 已按群号隔离；群聊需 @机器人 或 `/` 前缀唤醒）；观察群聊多人共享记忆库的行为是否符合预期。
3. **每日定时巩固窗口**（规范 §5.1 第三触发）：AstrBot 有 cron 体系，可在适配器注册每日任务调 consolidate。
4. **持久化运行**：目前 AstrBot/NapCat 都挂在后台 job 里，重启机器后需手动按 §4 拉起；可做成计划任务/NSSM 服务或 `astrbot run -r` + 开机自启。
5. 可选：真实 tokenizer、更强 embedding Provider、`http` 后端多宿主共享部署。
6. 搁置：DSH 插件加载修复（等有需要再按坑 4 方案修）。

---

## 8. 交接要点速查

- **跑测试**：`python -m unittest discover -s tests -v`（应 30/30 OK，3.11/3.12 均可）
- **启动全部**：`$env:DEEPSEEK_API_KEY="<key>"; .\scripts\start-all.ps1 [QQ号]`（工作目录：仓库根；AstrBot 运行目录固定为 `D:\projects_software\astrbot-runtime`）
- **停止全部**：`.\scripts\stop-all.ps1`
- **查看状态**：`.\scripts\status-all.ps1`
- **验证记忆**：暗号三步法见 §5（WebChatUI 与 QQ 两套均已验证）
- **AstrBot dashboard**：`http://127.0.0.1:6185`（密码见启动横幅/环境变量方式见 §4）
- **机器人 QQ**：`1178469499`（NapCat WebUI：`http://127.0.0.1:6099/webui`，token 见 NapCat 启动日志）
- **记忆库数据**：`D:\projects_software\astrbot-runtime\data\extra_brain\`（每 peer 一个 .db；换宿主=拷贝此目录）
- **本地离线体验**（不接宿主）：`extra-brain --db memory.db chat --threshold 8`
- **挂载新宿主**：读 `docs/MOUNT.md`，约 1 小时
