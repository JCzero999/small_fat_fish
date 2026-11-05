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
import random
import re
import sys
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
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

    def __init__(self, src_path: str, data_dir: str, companion_db: str = "", user_config: dict | None = None) -> None:
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
        self._companion_db = companion_db
        self._brains: dict[str, Any] = {}
        self._consolidate_locks: dict[str, threading.Lock] = {}
        # v0.10.0: user_config.json — 人设 / 安全规范 / API Key / 主动开口提示词
        self._user_config = user_config or {}

    @property
    def extractor_ready(self) -> bool:
        api_key = self._user_config.get("api", {}).get("api_key", "").strip()
        return bool(api_key or os.environ.get("DEEPSEEK_API_KEY"))

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "local",
            "data_dir": str(self.data_dir.resolve()),
            "extractor_ready": self.extractor_ready,
        }

    def _brain(self, peer_id: str):
        brain = self._brains.get(peer_id)
        if brain is None:
            extractor = None
            if self.extractor_ready:
                try:
                    extractor = self._extractor_cls.from_config(
                        self._user_config.get("api", {})
                    )
                except Exception:
                    # 回退环境变量
                    try:
                        extractor = self._extractor_cls.from_env()
                    except Exception:
                        pass
            # v0.10.0: 从 user_config 覆盖 Identity 默认值
            identity_override = self._user_config.get("identity")
            brain = self._ExtraBrain(
                self.data_dir / f"{_filename_for_peer(peer_id)}.db",
                extractor=extractor,
                companion_db=self._companion_db or None,
                safety_rules=self._user_config.get("safety_rules"),
                shadow_mode=bool(self._user_config.get("shadow_mode", False)),
                honesty_footer=bool(self._user_config.get("honesty_footer", False)),
            )
            # 覆盖 Identity：在创建后写入 Core
            if identity_override:
                try:
                    core = brain.core
                    changed = False
                    for field in ("name", "persona_core", "relationship_definition", "speaking_style"):
                        val = identity_override.get(field)
                        if val is not None and getattr(core.identity, field) != str(val):
                            setattr(core.identity, field, str(val))
                            changed = True
                    if changed:
                        brain.save_core(core)
                except Exception:
                    pass
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

    def ensure_proactive(self, peer_id: str) -> dict:
        brain = self._brain(peer_id)
        brain.ensure_proactive_config(peer_id)
        return brain.build_proactive_prompt(peer_id)

    def proactive_prompt(
        self, peer_id: str, now, force: bool = False, local_hour: int | None = None
    ) -> dict:
        return self._brain(peer_id).build_proactive_prompt(
            peer_id, now=now, force=force, local_hour=local_hour
        )

    def proactive_record(self, peer_id: str, now, umo: str | None = None) -> None:
        self._brain(peer_id).record_proactive_open(peer_id, now=now, umo=umo)

    def confirm_goal(self, peer_id: str, goal_id: str, now) -> bool:
        return self._brain(peer_id).confirm_goal_reminder(peer_id, goal_id, now=now)

    def confirm_sudden(self, peer_id: str, memory_id: str, now) -> None:
        self._brain(peer_id).confirm_sudden_recall(peer_id, memory_id, now=now)

    def shadow_status(self, peer_id: str) -> bool:
        return self._brain(peer_id).shadow_mode

    def set_shadow(self, peer_id: str, enabled: bool) -> bool:
        return self._brain(peer_id).set_shadow_mode(enabled)

    # ── v0.12.0 B 牢固度 ─────────────────────────────────────────

    def set_frozen(self, peer_id: str, memory_id: str, *, frozen: bool) -> dict:
        return self._brain(peer_id).set_memory_frozen(memory_id, frozen=frozen)

    def run_decay(self, peer_id: str) -> dict:
        return self._brain(peer_id).batch_decay()

    def set_proactive_enabled(self, peer_id: str, enabled: bool) -> bool:
        config = self._brain(peer_id).ensure_proactive_config(peer_id)
        config.enabled = enabled
        self._brain(peer_id).save_proactive_config(config)
        return enabled

    # ── v0.7.0 SelfState ──────────────────────────────────────────

    def self_state(self, peer_id: str) -> dict:
        return self._brain(peer_id).get_self_state().to_dict()

    # ── v0.8.0 DispositionSeed ────────────────────────────────────

    def list_dispositions(self, peer_id: str) -> list[dict]:
        return self._brain(peer_id).list_dispositions()

    def abstract_dispositions(self, peer_id: str, limit: int = 3) -> list[dict]:
        return self._brain(peer_id).abstract_dispositions(limit=limit)

    # ── v0.9.0 人设同步 + 人生快照 ────────────────────────────────

    def sync_persona(self, peer_id: str) -> dict:
        return self._brain(peer_id).sync_persona_to_companion()

    def pull_persona(self, peer_id: str) -> dict:
        return self._brain(peer_id).pull_persona_from_companion()

    def take_snapshot(self, peer_id: str, reason: str = "") -> str:
        return self._brain(peer_id).take_snapshot(reason=reason)

    def list_snapshots(self, peer_id: str) -> list[dict]:
        return self._brain(peer_id).list_snapshots()

    def rollback_snapshot(self, peer_id: str, snapshot_id: str) -> dict:
        return self._brain(peer_id).rollback_to_snapshot(snapshot_id)

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

    def ensure_proactive(self, peer_id: str) -> dict:
        return self._post("/proactive", {"peer_id": peer_id, "ensure": True})

    def proactive_prompt(self, peer_id: str, now, force: bool = False, local_hour: int | None = None) -> dict:
        from datetime import timezone as _tz

        body: dict[str, Any] = {"peer_id": peer_id, "force": force}
        if now is not None:
            body["now"] = now.astimezone(_tz.utc).isoformat()
        if local_hour is not None:
            body["local_hour"] = int(local_hour)
        return self._post("/proactive", body)

    def proactive_record(self, peer_id: str, now, umo: str | None = None) -> None:
        from datetime import timezone as _tz

        body: dict[str, Any] = {"peer_id": peer_id}
        if now is not None:
            body["now"] = now.astimezone(_tz.utc).isoformat()
        if umo is not None:
            body["umo"] = umo
        self._post("/proactive_record", body)

    def confirm_goal(self, peer_id: str, goal_id: str, now) -> bool:
        from datetime import timezone as _tz

        body: dict[str, Any] = {"peer_id": peer_id, "goal_id": goal_id}
        if now is not None:
            body["now"] = now.astimezone(_tz.utc).isoformat()
        return bool(self._post("/confirm_goal", body).get("marked"))

    def confirm_sudden(self, peer_id: str, memory_id: str, now) -> None:
        from datetime import timezone as _tz

        body: dict[str, Any] = {"peer_id": peer_id, "memory_id": memory_id}
        if now is not None:
            body["now"] = now.astimezone(_tz.utc).isoformat()
        self._post("/confirm_sudden", body)

    def shadow_status(self, peer_id: str) -> bool:
        return bool(self._get(f"/shadow?peer_id={urllib.request.quote(peer_id)}").get("shadow_mode"))

    def set_shadow(self, peer_id: str, enabled: bool) -> bool:
        return bool(self._post("/shadow", {"peer_id": peer_id, "enabled": enabled}).get("shadow_mode"))

    # ── v0.12.0 B 牢固度 ─────────────────────────────────────────

    def set_frozen(self, peer_id: str, memory_id: str, *, frozen: bool) -> dict:
        return self._post("/freeze", {"peer_id": peer_id, "memory_id": memory_id, "frozen": frozen})

    def run_decay(self, peer_id: str) -> dict:
        return self._post("/decay", {"peer_id": peer_id})

    def set_proactive_enabled(self, peer_id: str, enabled: bool) -> bool:
        raise NotImplementedError("http 后端暂不支持 enable/disable，请使用 local 后端")

    # ── v0.7.0–v0.9.0 sidecar 端点 ────────────────────────────────

    def self_state(self, peer_id: str) -> dict:
        return self._get(f"/self_state?peer_id={urllib.request.quote(peer_id)}")

    def list_dispositions(self, peer_id: str) -> list[dict]:
        return self._get(f"/dispositions?peer_id={urllib.request.quote(peer_id)}")

    def abstract_dispositions(self, peer_id: str, limit: int = 3) -> list[dict]:
        return self._post("/abstract-dispositions", {"peer_id": peer_id, "limit": limit})

    def sync_persona(self, peer_id: str) -> dict:
        return self._post("/sync-persona", {"peer_id": peer_id})

    def pull_persona(self, peer_id: str) -> dict:
        return self._post("/pull-persona", {"peer_id": peer_id})

    def take_snapshot(self, peer_id: str, reason: str = "") -> str:
        return self._post("/snapshot", {"peer_id": peer_id, "reason": reason}).get("snapshot_id", "")

    def list_snapshots(self, peer_id: str) -> list[dict]:
        return self._get(f"/snapshots?peer_id={urllib.request.quote(peer_id)}")

    def rollback_snapshot(self, peer_id: str, snapshot_id: str) -> dict:
        return self._post("/snapshot-rollback", {"peer_id": peer_id, "snapshot_id": snapshot_id})

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
        # 主动开口：peer_id -> (最近一次人类消息时间, 最近 umo)，进程重启即重置
        self._last_human_at: dict[str, datetime] = {}
        self._last_umo: dict[str, str] = {}
        self._proactive_task: asyncio.Task | None = None

        # v0.10.0: 加载 user_config.json（人设 / 安全规范 / API Key / 主动开口提示词）
        self._user_config = self._load_user_config()
        # v0.12.1 T1: 人设正文（persona_file 指向的 md；缺省空=沿用 identity 旧路径）
        self._persona_text = self._load_persona(self._user_config)
        # v0.12.1 T4: 诚实契约行开关（engine 侧同名参数）
        self._honesty_footer = bool(self._user_config.get("honesty_footer", False))
        if self._persona_text:
            logger.info(f"extra-brain: persona 正文已加载（{len(self._persona_text)} 字）")

        backend_mode = str(self.config.get("backend", "local"))
        try:
            if backend_mode == "http":
                self.backend = HttpBackend(str(self.config.get("base_url", "")))
            else:
                self.backend = LocalBackend(
                    str(self.config.get("extra_brain_src_path", "") or ""),
                    str(self.config.get("data_dir", "data/extra_brain")),
                    companion_db=str(self.config.get("companion_db", "") or ""),
                    user_config=self._user_config,
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

    _USER_CONFIG_CANDIDATES = (
        Path("config/user_config.json"),
        Path("data/config/user_config.json"),
    )
    _user_config_dir: Path | None = None

    @classmethod
    def _load_user_config(cls) -> dict:
        """从 config/user_config.json 加载用户配置（不存在则无害 fallback）。"""
        for candidate in cls._USER_CONFIG_CANDIDATES:
            if candidate.is_file():
                try:
                    cls._user_config_dir = candidate.parent
                    with open(candidate, encoding="utf-8-sig") as fh:
                        return json.load(fh)
                except Exception:
                    pass
        cls._user_config_dir = None
        return {}

    @staticmethod
    def _resolve_persona_source(raw: str):
        """解析人设来源：md 文件路径（多 base 查找）或内联文本（persona_text）。"""
        if not raw:
            return None
        path = Path(raw)
        if path.is_absolute():
            candidates = [path]
        else:
            bases = [Path("."), Path("config"), Path("data/config")]
            if ExtraBrainPlugin._user_config_dir is not None:
                bases += [
                    ExtraBrainPlugin._user_config_dir,
                    ExtraBrainPlugin._user_config_dir.parent,
                ]
            candidates = [base / raw for base in bases]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return ("file", candidate)
            except OSError:
                continue
        return ("inline", raw)

    @classmethod
    def _load_persona(cls, user_config: dict) -> str:
        """v0.12.1 T1：人设正文。persona_file=md 路径（推荐，含注释剥离与字上限）；
        persona_text=内联短文本（≤300 字）。都缺省 → 空串，回退 identity 旧路径。"""
        import re as _re
        raw = str(user_config.get("persona_file", "") or "").strip()
        source = cls._resolve_persona_source(raw)
        if source is None:
            text = str(user_config.get("persona_text", "") or "").strip()
            return text[:500] if text else ""
        kind, value = source
        if kind == "inline":
            return str(value)[:500]
        try:
            text = value.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        # 去掉文末 <!-- ... --> 注释区与首个 --- 之前的说明头（不注入）
        text = _re.sub(r"<!--.*?-->\s*", "", text, flags=_re.S)
        parts = text.split("\n---\n", 1)
        if len(parts) == 2 and parts[0].lstrip().startswith("#"):
            text = parts[1].strip()
        # 结构约定：正文从【你是谁】起（标题/说明头不注入）
        anchor = text.find("【你是谁】")
        if anchor > 0:
            text = text[anchor:]
        return text[:3000] if text else ""

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

    # -- proactive: 主动开口 ------------------------------------------------

    @property
    def _proactive_enabled(self) -> bool:
        return bool(self.config.get("proactive_enabled", False))

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if self._proactive_enabled and self.backend is not None:
            self._proactive_task = asyncio.create_task(self._proactive_loop())
            logger.info("extra-brain: 主动开口循环已启动")

    def _proactive_config(self) -> dict[str, int]:
        def _num(key: str, default: int) -> int:
            try:
                return max(1, int(self.config.get(key, default)))
            except (TypeError, ValueError):
                return default
        return {
            "interval": _num("proactive_check_interval_minutes", 20),
            "chance": max(1, min(100, _num("proactive_chance_percent", 30))),
            "limit": _num("proactive_daily_limit", 5),
            "silence": _num("proactive_silence_minutes", 120),
            "quiet_start": max(0, min(23, _num("proactive_quiet_hours_start", 22))),
            "quiet_end": max(0, min(23, _num("proactive_quiet_hours_end", 8))),
        }

    async def _proactive_loop(self) -> None:
        pcfg = self._proactive_config()
        while True:
            try:
                await asyncio.sleep(pcfg["interval"] * 60)
                summary = await self._proactive_tick(pcfg)
                # 开口与未开口都记录：面板外也要能回答「最近为什么没说」
                logger.info(f"extra-brain: 主动开口轮询结果：{summary}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"extra-brain: 主动开口循环异常（下一轮重试）：{exc}")

    async def _proactive_tick(self, pcfg: dict[str, int] | None = None, *, force_peer: str | None = None) -> str:
        """一次开口尝试：概率骰 → 逐 peer 决策 → 生成台词 → 发送 → 记录。"""
        pcfg = pcfg or self._proactive_config()
        backend = self._backend_or_none()
        if backend is None:
            return "后端不可用"
        candidates = [force_peer] if force_peer else list(self._last_umo.keys())
        if not candidates:
            return "没有已知 peer（等对方先发过消息才认识）"
        now = datetime.now().astimezone()
        skipped: list[str] = []
        for peer_id in candidates:
            if not force_peer and random.randint(1, 100) > pcfg["chance"]:
                skipped.append(f"{peer_id}: 概率骰未中")
                continue
            try:
                decision = await asyncio.to_thread(
                    backend.proactive_prompt, peer_id, now, False, now.hour
                )
            except Exception as exc:
                skipped.append(f"{peer_id}: 决策失败 {exc}")
                continue
            if not decision.get("should"):
                skipped.append(f"{peer_id}: {decision.get('reason')}")
                continue
            prompt_text = decision.get("prompt") or ""
            umo = self._last_umo.get(peer_id)
            if not umo:
                skipped.append(f"{peer_id}: 无回复路由")
                continue
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo)
                base_prompt = self._user_config.get("proactive", {}).get(
                    "system_prompt",
                    "你在主动开启一段对话，像惦记朋友一样自然提起，只输出要发送的那句话本身。",
                )
                # v0.12.1 T1：人设正文带上，主动开口的口吻也是小豚的（H4）
                if self._persona_text:
                    base_prompt = f"{base_prompt}\n\n{self._persona_text}"
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt_text,
                    system_prompt=base_prompt,
                )
            except Exception as exc:
                logger.warning(f"extra-brain: 台词生成失败（peer={peer_id}）：{exc}")
                continue
            line = (getattr(resp, "completion_text", "") or "").strip()
            if not line:
                skipped.append(f"{peer_id}: 模型未产出台词")
                continue
            try:
                sent = await self.context.send_message(
                    umo, MessageChain(chain=[Plain(text=line)])
                )
            except Exception as exc:
                logger.warning(f"extra-brain: 发送失败（peer={peer_id}）：{exc}")
                continue
            if not sent:
                skipped.append(f"{peer_id}: 平台不支持主动发送")
                continue
            try:
                await asyncio.to_thread(backend.proactive_record, peer_id, now, umo)
                # v0.11.0：约定提醒 / 忽然想起投递确认（计入额度与冷却）
                if decision.get("goal_id"):
                    await asyncio.to_thread(
                        backend.confirm_goal, peer_id, decision["goal_id"], now
                    )
                if decision.get("sudden_recall_id"):
                    await asyncio.to_thread(
                        backend.confirm_sudden, peer_id, decision["sudden_recall_id"], now
                    )
            except Exception as exc:
                logger.warning(f"extra-brain: 开口记录失败（peer={peer_id}）：{exc}")
            logger.info(f"extra-brain: 主动开口 peer={peer_id} 内容={line[:40]}")
            return f"已向 {peer_id} 主动开口：{line[:60]}"
        return "本轮未开口（" + "；".join(skipped[:4]) + "）"

    # -- hook: 注入 + user 消息捕获 ----------------------------------------

    def _swap_persona(self, system_prompt: str | None) -> str:
        """人设正文注入：追加到现有 system_prompt 末尾（幂等，已含则跳过）。

        人设 md 首句「你是小豚…」声明优先于面板旧人格；回滚只需清空 persona_file 配置。
        """
        current = system_prompt or ""
        head = self._persona_text[:24]
        if head and head in current:
            return current
        return f"{current}\n\n{self._persona_text}" if current else self._persona_text

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每轮 LLM 调用前：捕获 user 文本 + 注入长期记忆（每 event 一次）。"""
        if event.get_extra(_INJECT_FLAG):
            return
        event.set_extra(_INJECT_FLAG, True)
        prompt = (req.prompt or "").strip()
        peer_id = self._peer_of(event)

        # 主动开口依据：记录人类活跃时间与回复路由
        self._last_human_at[peer_id] = datetime.now().astimezone()
        self._last_umo[peer_id] = event.unified_msg_origin

        # v0.12.1 T1：人设正文注入（persona_file 配置时替换人格 prompt；每 event 一次）
        if self._persona_text:
            req.system_prompt = self._swap_persona(req.system_prompt)

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
        """长期记忆管理指令组
        v0.12.0 全指令：status consolidate core list search stats forget
        proactive self shadow freeze unfreeze disposition abstract sync-persona
        pull-persona snapshot snapshots snapshot-rollback
        """

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
            # v0.12.0：冻结（淡忘）标注 + retrievability 一位小数
            faded = ""
            if kind in ("semantic", "episodic"):
                if item.get("frozen"):
                    faded = " 〔已淡忘〕"
                elif "retrievability" in item:
                    faded = f" 〔牢度{float(item['retrievability']):.1f}〕"
            if kind == "semantic":
                lines.append(
                    f"[{item['id']}] {item['type']} "
                    f"{item['subject']}/{item['predicate']}/{item['object']}{faded}"
                )
            elif kind == "episodic":
                lines.append(f"[{item['id']}] {item['timestamp'][:10]} {item['summary']}{faded}")
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
        solidity = stats.get("solidity") or {}
        solidity_line = ""
        if solidity:
            solidity_line = (
                f"牢度：高 {solidity.get('high', 0)} / 中 {solidity.get('mid', 0)} / "
                f"低 {solidity.get('low', 0)} · 已淡忘 {solidity.get('frozen', 0)}"
            )
        lines = [
            "记忆库分层统计",
            f"总量：{stats.get('total_memories')} 条（归档 {stats.get('total_archived')}，"
            f"比例 {round(float(stats.get('archive_ratio', 0)) * 100, 1)}%）",
            f"语义：{semantic.get('total')} 条 | {tier_line(semantic.get('tiers', {}))}",
            f"情节：{episodic.get('total')} 条 | {tier_line(episodic.get('tiers', {}))}",
            f"风格：{stats.get('style', {}).get('total')} 条 | 向量 {stats.get('vectors', {}).get('total')} 个",
        ]
        if solidity_line:
            lines.append(solidity_line)
        lines.append(f"库体积：{round(int(stats.get('db_size_bytes', 0)) / 1024, 1)} KB")
        yield event.plain_result("\n".join(lines))

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

    @memory_group.command("proactive")
    async def memory_proactive(self, event: AstrMessageEvent, action: str = "status"):
        """主动开口：/memory proactive enable|disable|status|test"""
        peer_id = self._peer_of(event)
        backend = self._backend_or_none()
        if action in ("enable", "disable"):
            if backend is None:
                yield event.plain_result("后端不可用")
                return
            want_on = action == "enable"
            try:
                enabled = await asyncio.to_thread(
                    backend.set_proactive_enabled, peer_id, want_on
                )
            except NotImplementedError as exc:
                yield event.plain_result(str(exc))
                return
            except Exception as exc:
                yield event.plain_result(f"设置失败：{exc}")
                return
            yield event.plain_result(
                f"peer {peer_id} 的主动开口记忆配置已{'开启' if enabled else '关闭'}。\n"
                + ("注意：总开关还需在插件配置 proactive_enabled=true 并重启生效。"
                   if want_on else "")
            )
            return
        if action == "test":
            if backend is None:
                yield event.plain_result("后端不可用")
                return
            summary = await self._proactive_tick(force_peer=peer_id)
            yield event.plain_result(f"主动开口测试：{summary}")
            return
        # status
        pcfg = self._proactive_config()
        lines = [
            "主动开口状态",
            f"总开关（插件配置）：{'开' if self._proactive_enabled else '关'}",
            f"循环运行中：{'是' if self._proactive_task and not self._proactive_task.done() else '否'}",
            f"节奏：每 {pcfg['interval']} 分钟检查 · "
            f"概率 {pcfg['chance']}% · "
            f"每日 ≤{pcfg['limit']} 次 · "
            f"静默 ≥{pcfg['silence']} 分钟",
            f"安静时段：{pcfg['quiet_start']}:00 – {pcfg['quiet_end']}:00",
        ]
        if backend is not None:
            try:
                now = datetime.now().astimezone()
                decision = await asyncio.to_thread(
                    backend.proactive_prompt, peer_id, None, False, now.hour
                )
                if decision.get("should"):
                    lines.append(f"当前判定：可以开口（由头 {decision.get('reason')}）")
                else:
                    lines.append(f"当前判定：暂不开口，原因：{decision.get('reason')}")
            except Exception as exc:
                lines.append(f"当前判定：读取失败 {exc}")
        yield event.plain_result("\n".join(lines))

    # ── v0.7.0 /memory self ──────────────────────────────────────

    @memory_group.command("self")
    async def memory_self(self, event: AstrMessageEvent):
        """查看当前自我状态（精力 / 情绪 / 目标）"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            state = await asyncio.to_thread(backend.self_state, self._peer_of(event))
        except Exception as exc:
            yield event.plain_result(f"读取失败：{exc}")
            return
        energy = state.get("energy", 0.85)
        if energy >= 0.75:
            energy_label = "精力充沛"
        elif energy >= 0.40:
            energy_label = "状态平稳"
        elif energy >= 0.25:
            energy_label = "有点累了"
        else:
            energy_label = "很疲惫"
        goals = state.get("active_goals", [])

        def goal_line(g) -> str:
            if isinstance(g, dict):
                text = g.get("text", "")
                deadline = g.get("deadline")
                status = g.get("status", "open")
                if deadline:
                    try:
                        local = datetime.fromisoformat(deadline.replace("Z", "+00:00")).astimezone()
                        return f"  · {text}（约定 {local:%m月%d日%H:%M}｜{status}）"
                    except ValueError:
                        return f"  · {text}（约定 {deadline}｜{status}）"
                return f"  · {text}（{status}）" if status != "open" else f"  · {g.get('text', '')}"
            return f"  · {g}"

        goals_text = "\n".join(goal_line(g) for g in goals) if goals else "  · （暂无特别惦记的事）"
        fatigue = state.get("fatigue_reason")
        fatigue_text = f"最近疲惫原因：{fatigue}\n" if fatigue else ""
        try:
            shadow = await asyncio.to_thread(backend.shadow_status, self._peer_of(event))
        except Exception:
            shadow = False
        shadow_tag = "（影子模式：已记录，未注入）" if shadow else ""
        yield event.plain_result(
            "\n".join([
                "【这一刻的自己】",
                f"精力：{energy:.2f}（{energy_label}）",
                f"心情底色：{state.get('mood_baseline', '平静')}",
                fatigue_text,
                f"我在意的事：",
                goals_text,
                f"自我叙事：{state.get('self_narrative') or '（尚无）'}",
                f"版本：{state.get('version', 1)}{shadow_tag}",
            ]).rstrip()
        )

    # ── v0.11.0 /memory shadow ────────────────────────────────────

    @memory_group.command("shadow")
    async def memory_shadow(self, event: AstrMessageEvent, action: str = "status"):
        """影子模式：SelfState/倾向照常记录但不注入（on|off|status）"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        peer_id = self._peer_of(event)
        act = (action or "status").strip().lower()
        try:
            if act in ("on", "开启", "开"):
                enabled = await asyncio.to_thread(backend.set_shadow, peer_id, True)
                yield event.plain_result(
                    "影子模式已开启：自我状态照常记录，但不再注入到对话（记忆/人设不受影响）。"
                    if enabled else "切换失败"
                )
                return
            if act in ("off", "关闭", "关"):
                enabled = await asyncio.to_thread(backend.set_shadow, peer_id, False)
                yield event.plain_result(
                    "影子模式已关闭：从下一轮起注入【这一刻的自己】与内在声音。"
                    if not enabled else "切换失败"
                )
                return
            shadow = await asyncio.to_thread(backend.shadow_status, peer_id)
            yield event.plain_result(
                "影子模式：" + ("开（已记录，未注入）" if shadow else "关（正常注入）")
                + "\n切换：/memory shadow on|off"
            )
        except PermissionError:
            yield event.plain_result("当前为只读模式，无法切换")
        except Exception as exc:
            yield event.plain_result(f"操作失败：{exc}")

    # ── v0.12.0 B /memory freeze / unfreeze ──────────────────────

    @memory_group.command("freeze")
    async def memory_freeze(self, event: AstrMessageEvent, memory_id: str = ""):
        """手动冻结一条记忆（淡忘：不进自动注入，搜索仍可查到）"""
        await self._set_frozen(event, memory_id, frozen=True)

    @memory_group.command("unfreeze")
    async def memory_unfreeze(self, event: AstrMessageEvent, memory_id: str = ""):
        """手动解冻一条记忆（retrievability 重置为 1.0）"""
        await self._set_frozen(event, memory_id, frozen=False)

    async def _set_frozen(self, event: AstrMessageEvent, memory_id: str, *, frozen: bool):
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        if not (memory_id or "").strip():
            yield event.plain_result("用法：/memory freeze|unfreeze <记忆ID>")
            return
        try:
            result = await asyncio.to_thread(
                backend.set_frozen, self._peer_of(event), memory_id.strip(), frozen=frozen
            )
        except KeyError:
            yield event.plain_result(f"未找到记忆：{memory_id}（仅支持 semantic/episodic）")
            return
        except Exception as exc:
            yield event.plain_result(f"操作失败：{exc}")
            return
        state = "已淡忘（冻结）" if result.get("frozen") else "已恢复（牢度重置 1.0）"
        yield event.plain_result(f"[{result.get('id')}] {state}")

    # ── v0.8.0 /memory disposition / abstract ────────────────────

    @memory_group.command("disposition")
    async def memory_disposition(self, event: AstrMessageEvent):
        """列出当前倾向种子"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            seeds = await asyncio.to_thread(backend.list_dispositions, self._peer_of(event))
        except Exception as exc:
            yield event.plain_result(f"读取失败：{exc}")
            return
        if not seeds:
            yield event.plain_result("当前无倾向种子")
            return
        lines = ["当前倾向种子"]
        for s in seeds:
            tag = s.get("tag", "?"); val = s.get("value", 0)
            cat = s.get("category", "?"); src = s.get("source", "?")
            lines.append(f"  [{s.get('id','?')[:8]}] {tag} ({cat}) {val:+.02f} ← {src}")
        yield event.plain_result("\n".join(lines))

    @memory_group.command("abstract")
    async def memory_abstract(self, event: AstrMessageEvent):
        """从记忆中抽象新倾向种子（规则版，无需 LLM）"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            seeds = await asyncio.to_thread(backend.abstract_dispositions, self._peer_of(event))
        except Exception as exc:
            yield event.plain_result(f"抽象失败：{exc}")
            return
        if not seeds:
            yield event.plain_result("未产生新倾向种子（可能已有足够倾向或记忆不足）")
            return
        lines = [f"抽象出 {len(seeds)} 条新倾向种子"]
        for s in seeds:
            lines.append(f"  [{s.get('id','?')[:8]}] {s.get('tag','?')} ({s.get('category','?')}) {s.get('value',0):+.02f}")
        yield event.plain_result("\n".join(lines))

    # ── v0.9.0 人设同步 ─────────────────────────────────────────

    @memory_group.command("sync-persona")
    async def memory_sync_persona(self, event: AstrMessageEvent):
        """把私聊人设推入全局伴侣层（群聊同时生效）"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            result = await asyncio.to_thread(backend.sync_persona, self._peer_of(event))
        except Exception as exc:
            yield event.plain_result(f"同步失败：{exc}")
            return
        yield event.plain_result(
            f"人设已推入全局伴侣层\n→ {result.get('pushed_to', '（未知路径）')}\n→ 锚版本：{result.get('anchor', '—')}"
        )

    @memory_group.command("pull-persona")
    async def memory_pull_persona(self, event: AstrMessageEvent):
        """从全局伴侣库拉取人设到本 peer"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            result = await asyncio.to_thread(backend.pull_persona, self._peer_of(event))
        except Exception as exc:
            yield event.plain_result(f"拉取失败：{exc}")
            return
        yield event.plain_result(
            f"已从全局伴侣层拉取人设\n→ 来源：{result.get('pulled_from', '（未知）')}"
        )

    # ── v0.9.0 人生快照 ─────────────────────────────────────────

    @memory_group.command("snapshot")
    async def memory_snapshot(self, event: AstrMessageEvent, reason: str = ""):
        """保存当前自我状态为人生快照"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            sid = await asyncio.to_thread(backend.take_snapshot, self._peer_of(event), reason)
        except Exception as exc:
            yield event.plain_result(f"快照失败：{exc}")
            return
        yield event.plain_result(f"人生快照已保存\n→ id：{sid}")

    @memory_group.command("snapshots")
    async def memory_snapshots(self, event: AstrMessageEvent):
        """列出所有人生快照"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            snaps = await asyncio.to_thread(backend.list_snapshots, self._peer_of(event))
        except Exception as exc:
            yield event.plain_result(f"读取失败：{exc}")
            return
        if not snaps:
            yield event.plain_result("暂无人生快照")
            return
        lines = ["人生快照"]
        for s in snaps:
            lines.append(f"  [{s.get('id','?')[:8]}] {s.get('created_at','?')} {s.get('reason','')}")
        yield event.plain_result("\n".join(lines))

    @memory_group.command("snapshot-rollback")
    async def memory_snapshot_rollback(self, event: AstrMessageEvent, snapshot_id: str):
        """从指定快照回滚"""
        backend = self._backend_or_none()
        if backend is None:
            yield event.plain_result("后端不可用")
            return
        try:
            result = await asyncio.to_thread(backend.rollback_snapshot, self._peer_of(event), snapshot_id)
        except Exception as exc:
            yield event.plain_result(f"回滚失败：{exc}")
            return
        yield event.plain_result(f"已回滚到快照 {snapshot_id}\n→ 版本：{result.get('version', '—')}")

    async def terminate(self) -> None:
        if self._proactive_task is not None:
            self._proactive_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self.backend is not None:
            await asyncio.to_thread(self.backend.close)
        logger.info("extra-brain: terminated")
