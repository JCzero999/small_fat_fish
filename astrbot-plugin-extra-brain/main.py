"""astrbot_plugin_extra_brain — 把 extra_brain 长期记忆挂载到 AstrBot。

这是一个「薄适配器」：只做挂载契约的翻译（peer 标识 / 注入点 / 巩固点 /
降级策略），不实现任何记忆业务逻辑——记忆模型、抽取、检索、衰减全部在
extra_brain 库（Python 进程内）或其 sidecar（HTTP）里。

挂载契约（对任何聊天宿主通用，见仓库 docs/MOUNT.md）：
  1. peer_id   稳定字符串，一人/一群 = 一个记忆库
  2. 注入点    每轮 LLM 调用前：query(+近期上下文) → 追加到 system_prompt
  3. 巩固点    一批对话完成后：messages → 深度抽取并巩固
  4. 降级      注入失败照常对话；巩固失败保留缓冲下轮重试

本文件刻意不出现记忆库领域词汇（importance/decay/embedding 等）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest

# GreedyStr：把指令后剩余的全部文本收进一个参数（多词 search / 带 flag 的 forget 需要）。
# 从 core 路径导入（AstrBot 官方内置指令同样用法）；万一版本变动则回退为普通 str。
try:
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover - 版本兼容兜底
    GreedyStr = str

_INJECT_FLAG = "extra_brain_injected"

# unified_msg_origin 形如 "<platform>:<MessageType>:<session_id>"，其中 webchat 的
# session_id 是 "webchat!<username>!<conversation_id>"，conversation_id 每开一个新
# 对话就变。auto 粒度下剥掉它，让同一 ChatUI 用户跨对话共享记忆
# （等价于 QQ 按 qq 号隔离、群聊按群号隔离）。
_WEBCHAT_SESSION = re.compile(r"^(webchat![^!]+)![\w-]+$")


def resolve_peer_id(umo: str, scope: str) -> str:
    """unified_msg_origin → 稳定 peer_id。"""
    if scope == "full_umo":
        return umo
    parts = umo.split(":", 2)
    if len(parts) == 3 and parts[0] == "webchat":
        match = _WEBCHAT_SESSION.match(parts[2])
        if match:
            return f"{parts[0]}:{parts[1]}:{match.group(1)}"
    return umo


def _filename_for_peer(peer_id: str) -> str:
    """peer_id → 合法且唯一的文件名（umo 含 ':' 等非法字符）。"""
    safe = re.sub(r"[^A-Za-z0-9_@.-]+", "_", peer_id).strip("_") or "peer"
    digest = hashlib.md5(peer_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:64]}-{digest}"


def _context_lines(contexts: list, turns: int) -> list[str]:
    """OpenAI 格式历史 → 检索用近期上下文（"role: content"）。"""
    lines = []
    for message in list(contexts or [])[-turns * 2 :]:
        role = message.get("role", "")
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if isinstance(content, str) and content.strip():
            lines.append(f"{role}: {content.strip()}")
    return lines[-turns * 2 :]


def _response_text(resp: LLMResponse | None) -> str:
    if resp is None:
        return ""
    text = getattr(resp, "completion_text", "") or ""
    if not text:
        chain = getattr(resp, "result_chain", None)
        parts = []
        for component in getattr(chain, "chain", None) or []:
            piece = getattr(component, "text", None)
            if isinstance(piece, str):
                parts.append(piece)
        text = "".join(parts)
    return text.strip()


# ---------------------------------------------------------------- backends


class LocalBackend:
    """进程内直连 extra_brain 库。每个 peer 一个 ExtraBrain。

    巩固（含可达 60s 的 LLM 抽取调用）用 per-peer 互斥锁串行化，
    防止同一记忆库的两批巩固读到彼此的旧状态而互相覆盖；
    注入不加锁——MemoryStore 内部的连接锁已保证单次读写原子性，
    与巩固写交错的 worst case 只是丢失一次激活计数，不损坏数据。
    """

    def __init__(self, src_path: str, data_dir: str) -> None:
        if src_path:
            src_path = os.path.abspath(src_path)
            if src_path not in sys.path:
                sys.path.insert(0, src_path)
        from extra_brain import ExtraBrain
        from extra_brain.extraction import OpenAICompatibleExtractor

        self._ExtraBrain = ExtraBrain
        self._extractor_cls = OpenAICompatibleExtractor
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._brains: dict[str, Any] = {}
        self._consolidate_locks: dict[str, threading.Lock] = {}

    @property
    def extractor_ready(self) -> bool:
        return bool(os.environ.get("DEEPSEEK_API_KEY"))

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "local",
            "data_dir": str(self.data_dir.resolve()),
            "extractor_ready": self.extractor_ready,
        }

    def _brain(self, peer_id: str):
        brain = self._brains.get(peer_id)
        if brain is None:
            extractor = (
                self._extractor_cls.from_env() if self.extractor_ready else None
            )
            brain = self._ExtraBrain(
                self.data_dir / f"{_filename_for_peer(peer_id)}.db",
                extractor=extractor,
            )
            self._brains[peer_id] = brain
        return brain

    def inject(self, peer_id: str, query: str, recent: list[str], token_limit: int | None) -> dict:
        injection = self._brain(peer_id).build_injection(
            query, recent_context=recent, token_limit=token_limit
        )
        return {
            "context": injection.context,
            "estimated_tokens": injection.estimated_tokens,
            "memory_ids": injection.retrieved.memory_ids,
        }

    def consolidate(self, peer_id: str, messages: list[dict], session_id: str | None) -> dict:
        lock = self._consolidate_locks.setdefault(peer_id, threading.Lock())
        with lock:
            report = self._brain(peer_id).consolidate(messages, session_id=session_id)
        return report.to_dict()

    def core(self, peer_id: str) -> dict:
        return self._brain(peer_id).core.to_dict()

    def list(self, peer_id: str, kind: str) -> list[dict]:
        brain = self._brain(peer_id)
        if kind == "episodic":
            items = brain.store.list_episodic()
        elif kind == "style":
            items = brain.store.list_style()
        else:
            items = brain.store.list_semantic()
        return [item.to_dict() for item in items]

    def search(self, peer_id: str, query: str) -> dict:
        return self._brain(peer_id).retrieve(query).to_dict()

    def stats(self, peer_id: str) -> dict:
        """存储分层统计（返回库侧原样 payload，适配器不做领域判断）。"""
        return self._brain(peer_id).storage_stats()

    def forget(self, peer_id: str, memory_id: str, *, hard: bool, approved: bool) -> dict:
        return self._brain(peer_id).forget(memory_id, hard=hard, approved=approved)

    def close(self) -> None:
        for brain in self._brains.values():
            try:
                brain.close()
            except Exception:
                pass
        self._brains.clear()


class HttpBackend:
    """调用 extra_brain.server sidecar。零依赖（标准库 urllib）。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    @property
    def extractor_ready(self) -> bool:
        return True  # 由 sidecar 自行判断；巩固失败会在日志中显现

    def describe(self) -> dict[str, Any]:
        return {"backend": "http", "base_url": self.base_url}

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get(self, path: str) -> Any:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def inject(self, peer_id: str, query: str, recent: list[str], token_limit: int | None) -> dict:
        body: dict[str, Any] = {"peer_id": peer_id, "query": query, "recent_context": recent}
        if token_limit:
            body["token_limit"] = token_limit
        return self._post("/inject", body)

    def consolidate(self, peer_id: str, messages: list[dict], session_id: str | None) -> dict:
        return self._post(
            "/consolidate",
            {"peer_id": peer_id, "messages": messages, "session_id": session_id},
        )

    def core(self, peer_id: str) -> dict:
        return self._get(f"/core?peer_id={urllib.request.quote(peer_id)}")

    def list(self, peer_id: str, kind: str) -> list[dict]:
        return self._get(
            f"/list?peer_id={urllib.request.quote(peer_id)}&kind={kind}"
        ).get("items", [])

    def search(self, peer_id: str, query: str) -> dict:
        # sidecar 未提供独立 search 端点；local 后端可用
        raise NotImplementedError("http 后端暂不支持 search，请使用 local 后端")

    def stats(self, peer_id: str) -> dict:
        return self._get(f"/stats?peer_id={urllib.request.quote(peer_id)}")

    def forget(self, peer_id: str, memory_id: str, *, hard: bool, approved: bool) -> dict:
        return self._post(
            "/forget",
            {"peer_id": peer_id, "memory_id": memory_id, "hard": hard, "approved": approved},
        )

    def close(self) -> None:
        return None


# ---------------------------------------------------------------- plugin


class ExtraBrainPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.backend: Any = None
        self.import_error: str | None = None
        self._pending: dict[str, list[dict]] = {}  # peer_id -> 待巩固消息
        self._tasks: set = set()
        self._warned_once = False

        backend_mode = str(self.config.get("backend", "local"))
        try:
            if backend_mode == "http":
                self.backend = HttpBackend(str(self.config.get("base_url", "")))
            else:
                self.backend = LocalBackend(
                    str(self.config.get("extra_brain_src_path", "") or ""),
                    str(self.config.get("data_dir", "data/extra_brain")),
                )
        except Exception as exc:
            self.import_error = str(exc)
            logger.error(f"extra_brain 后端初始化失败：{exc}")

        info = self.backend.describe() if self.backend else {"backend": "unavailable"}
        logger.info(f"extra-brain: loaded {info}")

    # -- helpers ----------------------------------------------------------

    def _peer_of(self, event: AstrMessageEvent) -> str:
        return resolve_peer_id(
            event.unified_msg_origin, str(self.config.get("peer_scope", "auto"))
        )

    @property
    def _consolidate_every(self) -> int:
        try:
            value = int(self.config.get("consolidate_every", 8))
            return max(1, value)
        except (TypeError, ValueError):
            return 8

    def _backend_or_none(self):
        if self.backend is None and not self._warned_once:
            self._warned_once = True
            logger.warning(
                f"extra_brain 后端不可用（{self.import_error}），记忆功能暂停，对话不受影响"
            )
        return self.backend

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- hook: 注入 + user 消息捕获 ----------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每轮 LLM 调用前：捕获 user 文本 + 注入长期记忆（每 event 一次）。"""
        if event.get_extra(_INJECT_FLAG):
            return
        event.set_extra(_INJECT_FLAG, True)
        prompt = (req.prompt or "").strip()
        peer_id = self._peer_of(event)

        # 巩固用的 user 消息无条件捕获（不依赖 inject_enabled）
        if prompt:
            self._pending.setdefault(peer_id, []).append({"role": "user", "content": prompt})

        backend = self._backend_or_none()
        if backend is None or not self.config.get("inject_enabled", True) or not prompt:
            return

        recent = _context_lines(req.contexts, int(self.config.get("context_turns", 5) or 5))
        token_limit = int(self.config.get("token_limit", 0) or 0) or None

        try:
            result = await asyncio.to_thread(
                backend.inject, peer_id, prompt, recent, token_limit
            )
        except Exception as exc:
            logger.warning(f"extra-brain: 注入失败（peer={peer_id}）：{exc}")
            return

        context = result.get("context")
        if context:
            req.system_prompt = (
                f"{req.system_prompt}\n\n{context}" if req.system_prompt else str(context)
            )
            logger.debug(
                f"extra-brain: injected {result.get('estimated_tokens')} tokens "
                f"({len(result.get('memory_ids', []))} memories) into peer={peer_id}"
            )

    # -- hook: 巩固 -------------------------------------------------------

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM 回复完成后攒入待巩固缓冲；达到阈值触发后台巩固。"""
        backend = self._backend_or_none()
        if backend is None:
            return
        text = _response_text(resp)
        if not text:
            return
        peer_id = self._peer_of(event)
        pending = self._pending.setdefault(peer_id, [])
        pending.append({"role": "assistant", "content": text})

        if len(pending) // 2 >= self._consolidate_every:
            self._spawn(self._consolidate(peer_id, event.unified_msg_origin))

    async def _consolidate(self, peer_id: str, session_id: str | None) -> None:
        backend = self._backend_or_none()
        if backend is None:
            return
        messages = self._pending.get(peer_id, [])
        if not messages:
            return
        # 先取走缓冲再后台执行，避免本轮响应被分钟级抽取阻塞
        self._pending[peer_id] = []
        try:
            report = await asyncio.to_thread(
                backend.consolidate, peer_id, messages, session_id
            )
            logger.info(
                f"extra-brain: 巩固完成 peer={peer_id} "
                f"semantic={report.get('semantic_written')} "
                f"episodic={report.get('episodic_written')} "
                f"conflicts={len(report.get('conflicts', []))}"
            )
        except Exception as exc:
            # 失败放回缓冲，下轮重试
            self._pending[peer_id] = messages + self._pending[peer_id]
            logger.warning(f"extra-brain: 巩固失败（peer={peer_id}）：{exc}")

    # -- commands ---------------------------------------------------------

    @filter.command_group("memory")
    def memory_group(self):
        """长期记忆管理指令组"""

    @memory_group.command("status")
    async def memory_status(self, event: AstrMessageEvent):
        """查看记忆插件状态（peer、后端、待巩固轮数）"""
        peer_id = self._peer_of(event)
        if self.backend is not None:
            info = self.backend.describe()
            backend_line = (
                f"{info.get('backend')}（{info.get('data_dir') or info.get('base_url')}）"
            )
            extractor_line = (
                "已配置（DEEPSEEK_API_KEY）"
                if info.get("extractor_ready")
                else "未配置（只读，巩固不可用）"
            )
        else:
            backend_line = f"不可用（{self.import_error}）"
            extractor_line = "-"
        pending = len(self._pending.get(peer_id, [])) // 2
        yield event.plain_result(
            "\n".join(
                [
                    "「额外大脑」状态",
                    f"peer_id：{peer_id}",
                    f"后端：{backend_line}",
                    f"抽取模型：{extractor_line}",
                    f"待巩固轮数：{pending}",
                    f"自动巩固阈值：{self._consolidate_every} 轮",
                ]
            )
        )

    @memory_group.command("consolidate")
    async def memory_consolidate(self, event: AstrMessageEvent):
        """立即巩固当前 peer 的待巩固消息"""
        peer_id = self._peer_of(event)
        pending = self._pending.get(peer_id, [])
        if not pending:
            yield event.plain_result("（没有待巩固的消息）")
            return
        self._spawn(self._consolidate(peer_id, event.unified_msg_origin))
        yield event.plain_result(
            f"已触发巩固（{len(pending) // 2} 轮），完成后可在日志查看结果。"
        )

    @memory_group.command("core")
    async def memory_core(self, event: AstrMessageEvent):
        """查看当前 peer 的 Core Memory"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            core = await asyncio.to_thread(backend.core, self._peer_of(event))
        except Exception as exc:
            yield event.plain_result(f"读取失败：{exc}")
            return
        yield event.plain_result(json.dumps(core, ensure_ascii=False, indent=2))

    @memory_group.command("list")
    async def memory_list(self, event: AstrMessageEvent, kind: str = "semantic"):
        """列出记忆（semantic/episodic/style）"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        if kind not in ("semantic", "episodic", "style"):
            yield event.plain_result("kind 仅支持 semantic / episodic / style")
            return
        try:
            items = await asyncio.to_thread(backend.list, self._peer_of(event), kind)
        except Exception as exc:
            yield event.plain_result(f"读取失败：{exc}")
            return
        if not items:
            yield event.plain_result(f"（{kind} 记忆为空）")
            return
        lines = [f"共 {len(items)} 条 {kind} 记忆："]
        for item in items[:20]:
            if kind == "semantic":
                lines.append(
                    f"[{item['id']}] {item['type']} "
                    f"{item['subject']}/{item['predicate']}/{item['object']}"
                )
            elif kind == "episodic":
                lines.append(f"[{item['id']}] {item['timestamp'][:10]} {item['summary']}")
            else:
                lines.append(f"[{item['id']}] {item['category']} {item['observation']}")
        if len(items) > 20:
            lines.append("…（仅显示前 20 条）")
        yield event.plain_result("\n".join(lines))

    @memory_group.command("search")
    async def memory_search(self, event: AstrMessageEvent, query: GreedyStr):
        """检索记忆（本地后端）"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            result = await asyncio.to_thread(backend.search, self._peer_of(event), query)
        except NotImplementedError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:
            yield event.plain_result(f"检索失败：{exc}")
            return
        lines = []
        for item in result.get("semantic", [])[:5]:
            memory = item.get("memory", {})
            lines.append(
                f"[{memory.get('id')}] {memory.get('type')} "
                f"{memory.get('subject')}/{memory.get('predicate')}/{memory.get('object')}"
                f"（score={item.get('scores', {}).get('final_score')}）"
            )
        for item in result.get("episodic", [])[:2]:
            memory = item.get("memory", {})
            lines.append(f"[{memory.get('id')}] {memory.get('summary')}")
        yield event.plain_result("\n".join(lines) or "（没有相关记忆）")

    @memory_group.command("stats")
    async def memory_stats(self, event: AstrMessageEvent):
        """查看记忆库分层统计（数量/保留度档位/归档比例）"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            stats = await asyncio.to_thread(backend.stats, self._peer_of(event))
        except Exception as exc:
            yield event.plain_result(f"读取失败：{exc}")
            return
        tier_names = ["热(P5)", "温(P4)", "冷(P3)", "归档(P2)", "墓碑(P1)"]
        def tier_line(counts: dict) -> str:
            if not counts:
                return "（空）"
            return "  ".join(
                f"{tier_names[int(k)] if k.isdigit() and 0 <= int(k) < 5 else '档' + k}x{v}"
                for k, v in counts.items()
            )
        semantic = stats.get("semantic", {})
        episodic = stats.get("episodic", {})
        yield event.plain_result(
            "\n".join(
                [
                    "记忆库分层统计",
                    f"总量：{stats.get('total_memories')} 条（归档 {stats.get('total_archived')}，"
                    f"比例 {round(float(stats.get('archive_ratio', 0)) * 100, 1)}%）",
                    f"语义：{semantic.get('total')} 条 | {tier_line(semantic.get('tiers', {}))}",
                    f"情节：{episodic.get('total')} 条 | {tier_line(episodic.get('tiers', {}))}",
                    f"风格：{stats.get('style', {}).get('total')} 条 | 向量 {stats.get('vectors', {}).get('total')} 个",
                    f"库体积：{round(int(stats.get('db_size_bytes', 0)) / 1024, 1)} KB",
                ]
            )
        )

    @memory_group.command("forget")
    async def memory_forget(self, event: AstrMessageEvent, args: GreedyStr):
        """遗忘一条记忆：/memory forget <id>（默认 P1 墓碑）；<id> --hard --yes 才物理删除"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        tokens = (args or "").split()
        if not tokens:
            yield event.plain_result(
                "用法：/memory forget <记忆ID> [--hard --yes]（默认 P1 墓碑；--hard --yes 物理删除）"
            )
            return
        memory_id, flags = tokens[0], " ".join(tokens[1:])
        hard = "--hard" in flags
        approved = "--yes" in flags
        if hard and not approved:
            yield event.plain_result(
                "物理删除（P0）需要双确认：/memory forget <id> --hard --yes"
            )
            return
        try:
            result = await asyncio.to_thread(
                backend.forget, self._peer_of(event), memory_id, hard=hard, approved=approved
            )
        except PermissionError:
            yield event.plain_result("物理删除未获审批，已拒绝（适配器不会自动批准）。")
            return
        except KeyError:
            yield event.plain_result(f"记忆不存在：{memory_id}")
            return
        except Exception as exc:
            yield event.plain_result(f"遗忘失败：{exc}")
            return
        verb = "物理删除（P0）" if result.get("operation") == "hard_delete" else "墓碑化（P1）"
        yield event.plain_result(
            f"已{verb}：{memory_id}\n留档摘要：{result.get('digest', '')[:60]}"
        )

    # -- lifecycle --------------------------------------------------------

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.backend is not None:
            await asyncio.to_thread(self.backend.close)
        logger.info("extra-brain: terminated")
