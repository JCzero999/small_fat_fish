# dsh-plugin-extra-brain

DSH（DeepSeek Harness）/ Cordis 插件，把 extra_brain 长期记忆挂到宿主 Agent 上：每轮注入记忆、每轮/阈值时巩固。

记忆逻辑在 **Python sidecar**（`extra_brain.server`）里，本插件只做桥接。

## 结构

```
sidecar.js            # 纯 HTTP 客户端（无依赖，Node 18+ 的全局 fetch），可独立测试
index.js              # Cordis 插件 apply(ctx)：每轮注入记忆 + 每轮巩固
scripts/bridge-smoke.mjs  # Node↔Python 桥接冒烟（无需 DSH 即可验证）
```

DSH 加载的是编译后的 `.js`（本插件就是纯 ESM `.js`，无构建步骤），不要把 `exports` 指向 `.ts`。

## 快速验证桥接（不碰 DSH）

```bash
# 终端 1：起 sidecar（需要 DEEPSEEK_API_KEY）
export DEEPSEEK_API_KEY="..."
python -m extra_brain.server --data-dir /tmp/eb --port 8756

# 终端 2：跑桥接冒烟
node scripts/bridge-smoke.mjs http://127.0.0.1:8756
```

看到 `consolidate -> { semantic_written: 2, ... }` 且 inject 的 context 里出现「小舟」即桥接打通。

## 接入 DSH

`dsh plugin add` = 透传给 profile 目录里的 `pnpm add`。若 profile 里存在已下架的依赖（例如 `dsh-our-free-model-fish` 已被 npmjs 下架），任何 `pnpm add/install` 都会报 `ERR_PNPM_MALFORMED_METADATA`。此时**手动安装、绕过 pnpm** 更稳：

1. 把本插件放进 profile 的 `node_modules`（目录 junction 或直接拷贝），并在 profile `package.json` 的 `dsh.profile.bundles` 数组末尾追加 `"dsh-plugin-extra-brain"`：

   ```powershell
   # 目录 junction：插件留在仓库里可继续改，DSH 重启即生效
   cmd /c mklink /J "C:\Users\19758\.dsh\profiles\desktop\node_modules\dsh-plugin-extra-brain" "D:\projects\small_fat_fish\dsh-plugin-extra-brain"
   ```

   若 junction 解析不稳，退化为直接拷贝整个插件目录到 `node_modules\dsh-plugin-extra-brain`。

   这一步不触发 pnpm，死包完全不用动。

2. 在 profile 的 `cordis.patch.yml`（不是 `cordis.yml`）里按 `id` 覆盖 sidecar 地址与阈值：

   ```yaml
   - id: extra-brain
     name: dsh-plugin-extra-brain
     config:
       baseUrl: http://127.0.0.1:8756
       consolidateEvery: 1   # 1 = 每轮巩固
   ```

3. 确保 sidecar 常驻（可用 DSH 宿主生命周期或 systemd/计划任务拉起）。

## 绑定点（已对照本机 DSH 源码核实）

`index.js` 的绑定点是从你机器上 `app.asar` 里的 DSH 宿主源码核实过的：

- **注入**：`ctx.inject(['agents'], scope => scope.on('agent/pre-step', async ({ agent, step, signal }, next) => ...))` —— `next()` 返回 `decision`，往 `decision.messages` 追加一条 `createUserMessage(...)`（`@deepseek-ai/dsh-llm`）。每轮只在 `step === 1` 注入一次。
- **巩固**：`ctx.inject(['sessions'], scope => scope.on('session/event', (session, event) => ...))` —— 累计 `user/message`（仅 `source.kind === 'user'`）与 `assistant/message` 文本，`turn/end` 时冲刷到 sidecar。
- **peer 键**：`getOrCreateAnonymousUserId()`（`@deepseek-ai/dsh-anonymous-user-id`）返回**跨会话稳定**的匿名用户 ID（持久化于 `~/.dsh/.anonymous-user-id`），作为 `peer_id`。`session.id` 只用于「会话内待巩固消息」的缓冲键，**不是** peer 键——否则每个新会话就是一个新记忆库，跨会话根本记不住。
- **收尾**：`ctx.inject(['agents'], scope => scope.on('agent/disposed', ...))` 冲刷剩余消息（`agent.id === agent.session.id`，与缓冲键一致）。
- **关键坑（scope）**：`session/event` 与 `agent/*` 是**按 carrier scope 分发的事件，不是 root 广播**。插件必须用 `ctx.inject(['sessions'] / ['agents'], …)` 订阅，root 上的 `ctx.on(...)` 收不到（对照 `app.asar` 里 `dsh-deepseek-account.js` 的写法）。
- 注入的记忆消息带 `source.kind === 'extra-brain'`，不会被误当用户发言。

> ⚠️ **当前状态（2026-10-31）**：插件模块在 DSH 里尚未加载成功——顶部（import 之后）的 `mkdirSync(eb-markers)` 只要模块被加载就会建目录，实测不存在 = 模块没 load。根因：`index.js` `import` 了 `@deepseek-ai/dsh-anonymous-user-id` / `@deepseek-ai/dsh-llm` 却未声明 `peerDependencies`，DSH 对 linked 包的 `@deepseek-ai/*` 共享包解析失败。修复：把插件改**自包含**（内联 `getOrCreateAnonymousUserId` 与 `createUserMessage`、去掉内部包裸导入）+ 补 `peerDependencies`。详见 `../docs/HANDOVER.md` 坑 10。

## 隔离与存储

每个 peer 一个 SQLite：`<data_dir>/<peer_id>.db`，由 sidecar 管理。

- **当前最小挂载**：`peer_id` 固定为 DSH 的匿名用户 ID（`~/.dsh/.anonymous-user-id`）——「这台机器上一个用户 = 一个记忆库」，跨会话共享。
- **可选覆盖**：`config.peerId`（写在 `cordis.patch.yml`）可强制指定固定 peer，便于测试或显式隔离。
- **QQ 打通后**：把 `peer_id` 改成 QQ 用户/群标识（私聊按用户、群聊按群隔离），而不是匿名用户 ID。

## 验证宿主加载

本插件是**宿主（host-only）插件**，不会出现在客户端插件列表里（没有 `dsh.client` 入口）。确认它真的加载，看宿主日志里的启动行：

```
extra-brain: loaded (peerId=f9e05b96-…, sidecar=http://127.0.0.1:8756, consolidateEvery=1)
```

看到这行 = host 半加载成功。记忆是否生效，用「跨会话验证」见下。
