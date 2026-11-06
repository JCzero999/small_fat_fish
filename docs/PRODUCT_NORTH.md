# 小豚产品北极星（Product North）

> 摘自《小豚 v0.13.0 迭代文档》§0–§1。新贡献者读完本页即知：产品是什么、主场在哪、红线是什么。

## 一句话定位

给陪伴型 Agent「小豚」一套**少量、高相关、可解释、可遗忘**的跨会话长期记忆（`extra_brain`），
并提供一个**不依赖 QQ 的独立聊天主场（Chat Shell）**，人与 AI 编程工具共用同一套记忆后端。

## 主场定义（v0.13.0 起）

- **主场** = 本机 Web Chat Shell + Agent HTTP API（`http://127.0.0.1:8756/shell/`）。
- **QQ（AstrBot）可继续跑**，但文档与默认引导指向主场；QQ 是众多挂载点之一，不再是唯一入口。
- **人机同权**：UI 能做的巩固/注入预览，API 必须能做（见 `SHELL_API.md`）。

## 打开方式（新贡献者三步）

1. 启动：`python -m extra_brain.server --data-dir extra-brain`（默认 127.0.0.1:8756）
2. 浏览器打开 `http://127.0.0.1:8756/shell/`，粘贴 shell token（启动时打印或见 `config/user_config.json`）
3. 聊几句 → 点「记住以上对话」→ 开新会话 → 小豚记得你

## 成功标准

不打开 QQ，也能完成「多轮对话 → 巩固 → 新会话被记住」；且同一流程可用
`python scripts/shell_regression.py` 无 GUI 跑绿。

## 第一性原理（任何迭代不得违反）

1. **记忆唯一真相源** = 现有 `extra_brain` SQLite（按 peer）；禁止在前端另存对话当长期记忆。
2. **主场** = Web Shell + API；QQ 可继续跑，但文档与默认引导指向壳。
3. **人机同权** = UI 能做的巩固/注入预览，API 必须能做。
4. **默认本机** = 服务只绑 `127.0.0.1`；带本地 token；禁止默认 `0.0.0.0`。
5. **零强制迁移** = 旧 QQ peer 库不动；主壳使用独立默认 `peer_id`（`local-main`）。

## 非目标（不得自行扩大）

方案 C 自主记账（v0.14）、ASR/TTS 产品化、通用 Computer Use、桌面键鼠控制、
抖音/B 站爬取或自动发帖、3D 形象、修改 FSRS/情感状态机语义、重做 DSH 插件。
