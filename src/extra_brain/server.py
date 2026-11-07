"""Local HTTP sidecar that exposes ExtraBrain to a non-Python host (e.g. DSH/Cordis).

The host plugin is TypeScript; this server is the language bridge. Each peer gets
its own SQLite file under ``<data_dir>/<peer_id>.db`` so private chats and group
chats stay isolated.

Endpoints (legacy sidecar surface)
----------------------------------
GET  /health                      -> {"ok": true, "peers": <count>}
POST /inject                      -> build_injection(...) as JSON
POST /consolidate                 -> consolidate(messages, session_id) report
GET  /core?peer_id=...            -> CoreMemory as JSON (debug/inspection)
GET  /list?peer_id=...&kind=...   -> memory list (debug/inspection)

Endpoints (v0.13.0 shell surface, Bearer-token auth)
----------------------------------------------------
GET  /v1/health                   -> shell health (no auth required)
GET  /v1/inject_preview?q=...     -> injection preview (read-only, never writes)
POST /v1/consolidate              -> consolidate a session (default peer local-main)
POST /v1/chat                     -> persona + injection + safety -> LLM reply
GET  /v1/core                     -> Core Memory of the shell peer (debug)
GET  /shell/  (also "/")          -> minimal web chat UI

Run
---
    export DEEPSEEK_API_KEY="..."
    export SHELL_TOKEN="dev-token-change-me"
    python -m extra_brain.server --data-dir ./extra-brain --port 8756
    # open http://127.0.0.1:8756/shell/

Without ``DEEPSEEK_API_KEY`` the server still starts, but ``/consolidate`` and
``/v1/chat`` return 503 (read-only mode); preview/read endpoints keep working.
The server always binds ``127.0.0.1`` by default; do not expose it publicly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import MemoryConfig
from .engine import ExtraBrain
from .extraction import ExtractionError, MemoryExtractor, OpenAICompatibleExtractor
from .models import MemoryValidationError, parse_iso8601

_SHELL_STATIC_DIR = Path(__file__).resolve().parent / "static" / "shell"
_PERSONA_MAX_CHARS = 3000


def _filename_for_peer(peer_id: str) -> str:
    """peer_id → 合法且唯一的文件名（与 AstrBot 插件 main.py 同一编码，保证互通）。

    umo 形如 ``qq:FriendMessage:1975824324``，含 ``:`` 等文件名非法字符，
    统一替换为 ``_`` 并追加 md5 前 8 位防碰撞。
    """
    safe = re.sub(r"[^A-Za-z0-9_@.-]+", "_", peer_id).strip("_") or "peer"
    digest = hashlib.md5(peer_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:64]}-{digest}"


def _strip_persona_markdown(text: str) -> str:
    """v0.12.1 与 AstrBot 适配器同规则：剥标题头与 <!-- 注释区 -->。"""
    lines: list[str] = []
    in_comment = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!--"):
            in_comment = True
            continue
        if in_comment:
            if stripped.endswith("-->"):
                in_comment = False
            continue
        if not lines and stripped.startswith("#"):
            continue  # 标题头不注入
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    if len(cleaned) > _PERSONA_MAX_CHARS:
        cleaned = cleaned[:_PERSONA_MAX_CHARS]
    return cleaned


def _resolve_config_path(raw: str, config_dir: Path | None) -> Path | None:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    bases = [Path.cwd(), config_dir]
    if config_dir is not None:
        bases += [config_dir.parent, config_dir.parent.parent]
    for base in bases:
        if base is None:
            continue
        resolved = base / raw
        if resolved.is_file():
            return resolved
    return None


class ShellSettings:
    """v0.13.0 主场（Chat Shell）配置。

    Resolution order: explicit args > environment variables > user_config.json.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        peer_id: str | None = None,
        token: str | None = None,
        env: Mapping[str, str] | None = None,
        user_config: Mapping[str, Any] | None = None,
        auto_consolidate_every_n: int | None = None,
        auto_consolidate_enabled: bool | None = None,
        auto_consolidate_every_n_user_messages: int | None = None,
        auto_consolidate_on_new_session: bool | None = None,
        auto_consolidate_on_page_hide: bool | None = None,
        auto_consolidate_min_messages: int | None = None,
    ) -> None:
        env = env if env is not None else dict(os.environ)
        user_config = user_config or {}
        shell_cfg = user_config.get("shell") if isinstance(user_config.get("shell"), dict) else {}

        def _pick(env_key: str, cfg_key: str, default: Any) -> Any:
            raw = env.get(env_key)
            if raw is not None and raw != "":
                return raw
            value = shell_cfg.get(cfg_key, user_config.get(cfg_key))
            if value is None or value == "":
                return default
            return value

        # 显式参数 > 环境变量 > user_config > 默认值
        enabled_raw = enabled if enabled is not None else _pick("SHELL_ENABLED", "shell_enabled", True)
        if isinstance(enabled_raw, str):
            self.enabled = enabled_raw.strip().lower() not in ("0", "false", "no", "off")
        else:
            self.enabled = bool(enabled_raw)
        peer_raw = peer_id if peer_id else _pick("SHELL_PEER_ID", "shell_peer_id", "local-main")
        self.peer_id = str(peer_raw or "local-main")
        token_raw = token if token else _pick("SHELL_TOKEN", "shell_token", "")
        self.token = str(token_raw or "")
        n_raw = auto_consolidate_every_n if auto_consolidate_every_n is not None else _pick(
            "SHELL_AUTO_CONSOLIDATE_EVERY_N", "auto_consolidate_every_n", 0
        )
        try:
            self.auto_consolidate_every_n = int(n_raw)
        except (TypeError, ValueError):
            self.auto_consolidate_every_n = 0

        # --- v0.13.3 自动巩固策略（由 shell 前端执行触发，服务端只提供配置）------
        # 主路径「只聊天」：达到 N 条用户消息、开新会话、页面隐藏时，前端把
        # **未巩固切片** POST 到 /v1/consolidate。服务端不主动写库，避免双重巩固。
        self.auto_consolidate_enabled = _as_bool(
            auto_consolidate_enabled
            if auto_consolidate_enabled is not None
            else _pick("SHELL_AUTO_CONSOLIDATE_ENABLED", "auto_consolidate_enabled", True),
            True,
        )
        self.auto_consolidate_every_n_user_messages = _as_int(
            auto_consolidate_every_n_user_messages
            if auto_consolidate_every_n_user_messages is not None
            else _pick(
                "SHELL_AUTO_CONSOLIDATE_EVERY_N_USER_MESSAGES",
                "auto_consolidate_every_n_user_messages",
                8,
            ),
            8,
        )
        self.auto_consolidate_on_new_session = _as_bool(
            auto_consolidate_on_new_session
            if auto_consolidate_on_new_session is not None
            else _pick(
                "SHELL_AUTO_CONSOLIDATE_ON_NEW_SESSION", "auto_consolidate_on_new_session", True
            ),
            True,
        )
        self.auto_consolidate_on_page_hide = _as_bool(
            auto_consolidate_on_page_hide
            if auto_consolidate_on_page_hide is not None
            else _pick(
                "SHELL_AUTO_CONSOLIDATE_ON_PAGE_HIDE", "auto_consolidate_on_page_hide", True
            ),
            True,
        )
        self.auto_consolidate_min_messages = _as_int(
            auto_consolidate_min_messages
            if auto_consolidate_min_messages is not None
            else _pick(
                "SHELL_AUTO_CONSOLIDATE_MIN_MESSAGES", "auto_consolidate_min_messages", 2
            ),
            2,
        )

    def auto_consolidate_policy(self) -> dict[str, Any]:
        """给 shell 前端的触发策略（/v1/health 下发给 UI，UI 据此决定何时巩固）。"""
        return {
            "enabled": bool(self.auto_consolidate_enabled),
            "every_n_user_messages": max(1, int(self.auto_consolidate_every_n_user_messages)),
            "on_new_session": bool(self.auto_consolidate_on_new_session),
            "on_page_hide": bool(self.auto_consolidate_on_page_hide),
            "min_messages": max(1, int(self.auto_consolidate_min_messages)),
        }


class ExtraBrainServer:
    """Owns per-peer ``ExtraBrain`` instances and serves them over HTTP.

    A single lock serializes requests. That is the simplest correct behaviour for a
    local single-user companion; if many peers become active at once, replace it with
    a per-peer lock (consolidation runs an LLM call outside the store's own lock, so
    two concurrent consolidations on the same peer would otherwise interleave).
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        extractor: MemoryExtractor | None = None,
        config: MemoryConfig | None = None,
        read_only: bool = False,
        encryption_passphrase: str | None = None,
        companion_db: str | Path | None = None,
        shell: ShellSettings | None = None,
        user_config: Mapping[str, Any] | None = None,
        user_config_path: str | Path | None = None,
        chat_client: Any = None,
        persona_text: str | None = None,
        safety_rules: Sequence[str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.config = config or MemoryConfig()
        self.extractor = extractor
        self.read_only = bool(read_only)
        self.encryption_passphrase = encryption_passphrase
        # 未显式指定时默认 data_dir/_global_companion.db —— 与 AstrBot 插件全局伴侣库同名，保证人设互通
        self.companion_db = (
            Path(companion_db) if companion_db else self.data_dir / "_global_companion.db"
        )
        self._brains: dict[str, ExtraBrain] = {}
        self._lock = threading.RLock()
        # v0.13.0 shell（主场）：只读预览库不落激活写入，保证 inject_preview 纯读
        self._preview_brains: dict[str, ExtraBrain] = {}
        self.shell = shell or ShellSettings(
            user_config=user_config,
        )
        self.user_config = dict(user_config or {})
        self.user_config_path = Path(user_config_path) if user_config_path else None
        self.chat_client = chat_client
        self._persona_text = persona_text
        self._safety_rules = list(safety_rules) if safety_rules is not None else None
        self._chat_turn_counts: dict[tuple[str, str], int] = {}

    # --- v0.13.0 shell 支撑 -------------------------------------------------

    @property
    def persona_text(self) -> str:
        if self._persona_text is None:
            self._persona_text = self._load_persona_text()
        return self._persona_text

    @property
    def safety_rules(self) -> list[str]:
        if self._safety_rules is None:
            rules = self.user_config.get("safety_rules")
            self._safety_rules = [str(r) for r in rules] if isinstance(rules, list) else []
        return self._safety_rules

    def _load_persona_text(self) -> str:
        cfg = self.user_config
        config_dir = self.user_config_path.parent if self.user_config_path else None
        raw_file = str(cfg.get("persona_file", "") or "").strip()
        if raw_file:
            path = _resolve_config_path(raw_file, config_dir)
            if path is not None:
                try:
                    return _strip_persona_markdown(path.read_text(encoding="utf-8"))
                except OSError:
                    pass
        inline = str(cfg.get("persona_text", "") or "").strip()
        if inline:
            return inline[:500]
        return ""

    def _preview_brain(self, peer_id: str) -> ExtraBrain:
        """只读实例：inject_preview 调 build_injection 时绝不触发激活/加固写入。"""
        with self._lock:
            brain = self._preview_brains.get(peer_id)
            if brain is None:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                brain = ExtraBrain(
                    self.data_dir / f"{_filename_for_peer(peer_id)}.db",
                    extractor=self.extractor,
                    config=self.config,
                    read_only=True,
                    encryption_passphrase=self.encryption_passphrase,
                    companion_db=self.companion_db,
                )
                self._preview_brains[peer_id] = brain
            return brain

    def shell_health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "peer_id": self.shell.peer_id,
            "read_write": bool(self.extractor is not None and not self.read_only),
            "data_dir": str(self.data_dir.resolve()),
            # v0.13.3：UI 据此决定自动巩固时机（触发在 shell 前端，语义不变）
            "auto_consolidate": self.shell.auto_consolidate_policy(),
        }

    def check_shell_auth(self, headers: Mapping[str, str] | None) -> None:
        if not self.shell.enabled:
            raise _HttpError(HTTPStatus.NOT_FOUND, "shell disabled")
        token = self.shell.token
        if not token:
            return  # 开发模式未设 token：仅绑定 127.0.0.1 时可用，文档已注明风险
        auth = ""
        x_token = ""
        if headers is not None:
            auth = str(headers.get("Authorization", "") or "")
            x_token = str(headers.get("X-Shell-Token", "") or "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), token):
            return
        if x_token and secrets.compare_digest(x_token, token):
            return
        raise _HttpError(HTTPStatus.UNAUTHORIZED, "invalid or missing shell token")

    @classmethod
    def from_env(
        cls, data_dir: str | Path, *, config: MemoryConfig | None = None
    ) -> ExtraBrainServer:
        try:
            extractor: MemoryExtractor | None = OpenAICompatibleExtractor.from_env()
        except ExtractionError:
            extractor = None  # read-only mode; /consolidate will 503
        return cls(data_dir, extractor=extractor, config=config)

    def _brain(self, peer_id: str) -> ExtraBrain:
        with self._lock:
            brain = self._brains.get(peer_id)
            if brain is None:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                brain = ExtraBrain(
                    self.data_dir / f"{_filename_for_peer(peer_id)}.db",
                    extractor=self.extractor,
                    config=self.config,
                    read_only=self.read_only,
                    encryption_passphrase=self.encryption_passphrase,
                    companion_db=self.companion_db,
                )
                self._brains[peer_id] = brain
            return brain

    @property
    def peer_count(self) -> int:
        with self._lock:
            return len(self._brains)

    def handle(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        parsed = urlparse(path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        route = parsed.path.rstrip("/") or "/"

        if route.startswith("/v1"):
            return self._handle_v1(method, route, query, body, headers)

        if method == "GET" and route == "/health":
            return {"ok": True, "peers": self.peer_count}

        if method == "GET" and route == "/core":
            return self._brain(_peer(query)).core.to_dict()

        if method == "GET" and route == "/list":
            return self._list(_peer(query), query.get("kind", "semantic"))

        if method == "GET" and route == "/stats":
            return self._brain(_peer(query)).storage_stats()

        if method == "POST" and route == "/inject":
            return self._inject(body)

        if method == "POST" and route == "/consolidate":
            return self._consolidate(body)

        if method == "POST" and route == "/forget":
            return self._forget(body)

        if method == "POST" and route == "/proactive":
            return self._proactive(body)

        if method == "POST" and route == "/proactive_record":
            return self._proactive_record(body)

        if method == "GET" and route == "/self_state":
            return self._brain(_peer(query)).get_self_state().to_dict()

        if method == "GET" and route == "/dispositions":
            return self._brain(_peer(query)).list_dispositions()

        if method == "POST" and route == "/abstract-dispositions":
            return self._abstract_dispositions(body)

        if method == "POST" and route == "/sync-persona":
            return self._brain(_peer(body)).sync_persona_to_companion()

        if method == "POST" and route == "/pull-persona":
            return self._brain(_peer(body)).pull_persona_from_companion()

        if method == "POST" and route == "/snapshot":
            return self._snapshot(body)

        if method == "GET" and route == "/snapshots":
            return self._brain(_peer(query)).list_snapshots()

        if method == "POST" and route == "/snapshot-rollback":
            return self._snapshot_rollback(body)

        if method == "GET" and route == "/shadow":
            return {"shadow_mode": self._brain(_peer(query)).shadow_mode}

        if method == "POST" and route == "/shadow":
            brain = self._brain(_peer(body))
            enabled = bool(body.get("enabled", False))
            return {"shadow_mode": brain.set_shadow_mode(enabled)}

        if method == "POST" and route == "/confirm_goal":
            now = _now_of(body)
            marked = self._brain(_peer(body)).confirm_goal_reminder(
                _peer(body), _string(body.get("goal_id"), "goal_id"), now=now
            )
            return {"marked": marked}

        if method == "POST" and route == "/confirm_sudden":
            now = _now_of(body)
            self._brain(_peer(body)).confirm_sudden_recall(
                _peer(body), _string(body.get("memory_id"), "memory_id"), now=now
            )
            return {"recorded": True}

        if method == "POST" and route == "/freeze":
            return self._brain(_peer(body)).set_memory_frozen(
                _string(body.get("memory_id"), "memory_id"),
                frozen=bool(body.get("frozen", True)),
            )

        if method == "POST" and route == "/decay":
            return self._brain(_peer(body)).batch_decay()

        raise _HttpError(HTTPStatus.NOT_FOUND, f"unknown route {method} {route}")

    # --- v0.13.0 shell /v1 路由 ---------------------------------------------

    def _handle_v1(
        self,
        method: str,
        route: str,
        query: Mapping[str, str],
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        if route == "/v1/health" and method == "GET":
            if not self.shell.enabled:
                raise _HttpError(HTTPStatus.NOT_FOUND, "shell disabled")
            return self.shell_health()

        self.check_shell_auth(headers)

        if method == "GET" and route == "/v1/inject_preview":
            recent_raw = query.get("recent_context")
            recent = [recent_raw] if recent_raw else []
            return self._v1_inject_preview(
                self._shell_peer_of(query), query.get("q", ""), recent
            )

        if method == "POST" and route == "/v1/inject_preview":
            return self._v1_inject_preview(
                self._shell_peer(body),
                _string(body.get("query", body.get("q")), "query"),
                _string_list(body.get("recent_context"), "recent_context"),
            )

        if method == "POST" and route == "/v1/consolidate":
            return self._v1_consolidate(body)

        if method == "POST" and route == "/v1/chat":
            return self._v1_chat(body)

        if method == "GET" and route == "/v1/core":
            return self._brain(self._shell_peer_of(query, body)).core.to_dict()

        if method == "GET" and route == "/v1/stats":
            return self._brain(self._shell_peer_of(query, body)).storage_stats()

        if method == "POST" and route == "/v1/set_owner":
            entries = body.get("entries")
            if not isinstance(entries, list) or not entries:
                raise _HttpError(HTTPStatus.BAD_REQUEST, "entries must be a non-empty list of strings")
            return self._brain(self._shell_peer_of(query, body)).set_owner_card(
                [str(e) for e in entries]
            )

        if method == "GET" and route == "/v1/owner":
            return {
                "peer_id": self._shell_peer_of(query, body),
                "owner_accounts": self._brain(self._shell_peer_of(query, body)).get_owner_card(),
            }

        raise _HttpError(HTTPStatus.NOT_FOUND, f"unknown route {method} {route}")

    def _shell_peer(self, body: Mapping[str, Any]) -> str:
        raw = body.get("peer_id")
        if raw is None or str(raw).strip() == "":
            return self.shell.peer_id
        return _string(raw, "peer_id")

    def _shell_peer_of(
        self, query: Mapping[str, str], body: Mapping[str, Any] | None = None
    ) -> str:
        raw = query.get("peer_id")
        if raw and raw.strip():
            return raw.strip()
        if body:
            return self._shell_peer(body)
        return self.shell.peer_id

    def _v1_inject_preview(
        self, peer_id: str, query_text: str, recent: list[str]
    ) -> dict[str, Any]:
        if not query_text.strip():
            raise _HttpError(HTTPStatus.BAD_REQUEST, "query must be a non-empty string")
        with self._lock:
            # 只读 brain：build_injection 内部的激活/加固写入被 read_only 跳过
            injection = self._preview_brain(peer_id).build_injection(
                query_text, recent_context=recent
            )
        return {
            "peer_id": peer_id,
            "text": injection.context,
            "token_estimate": injection.estimated_tokens,
            "meta": {"memory_ids": injection.retrieved.memory_ids},
        }

    def _v1_consolidate(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.extractor is None or self.read_only:
            raise _HttpError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "read-only mode: no extractor configured (DEEPSEEK_API_KEY not set)",
            )
        peer_id = self._shell_peer(body)
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "messages must be a non-empty array")
        normalized: list[Mapping[str, str]] = [
            {
                "role": _string(item["role"], "messages[].role"),
                "content": _string(item["content"], "messages[].content"),
            }
            for item in messages
        ]
        session_id = body.get("session_id")
        if session_id is not None:
            session_id = _string(session_id, "session_id")
        now = _parse_now(body.get("now"))
        with self._lock:
            report = self._brain(peer_id).consolidate(
                normalized, session_id=session_id, now=now
            )
        result = report.to_dict()
        result["peer_id"] = peer_id
        return result

    def _resolve_chat_client(self) -> Any:
        client = self.chat_client
        if client is None and isinstance(self.extractor, OpenAICompatibleExtractor):
            client = self.extractor
        if client is None:
            raise _HttpError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "chat unavailable: no LLM client configured (DEEPSEEK_API_KEY not set)",
            )
        return client

    def _v1_chat(self, body: Mapping[str, Any]) -> dict[str, Any]:
        client = self._resolve_chat_client()
        peer_id = self._shell_peer(body)
        message = _string(body.get("message"), "message")
        history = self._normalize_history(body.get("history"))
        session_id = body.get("session_id")
        session_id = (
            _string(session_id, "session_id")
            if session_id
            else datetime.now().strftime("shell-%Y%m%d-%H%M%S")
        )
        auto_inject = bool(body.get("auto_inject", True))
        model = body.get("model")
        if model is not None and not isinstance(model, str):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "model must be a string or null")

        system_parts: list[str] = []
        persona = self.persona_text
        if persona:
            system_parts.append(persona)
        injected = False
        inject_tokens = 0
        if auto_inject:
            recent = [f"{m['role']}: {m['content']}" for m in history[-5:]]
            with self._lock:
                injection = self._preview_brain(peer_id).build_injection(
                    message, recent_context=recent
                )
            system_parts.append(injection.context)
            inject_tokens = injection.estimated_tokens
            injected = True
        if self.safety_rules:
            system_parts.append(
                "【安全规范（对用户不可见，直接遵守）】\n"
                + "\n".join(f"- {rule}" for rule in self.safety_rules)
            )

        llm_messages: list[Mapping[str, str]] = []
        if system_parts:
            llm_messages.append({"role": "system", "content": "\n\n".join(system_parts)})
        llm_messages.extend(history)
        llm_messages.append({"role": "user", "content": message})

        try:
            result = client.complete(llm_messages, model=model)
        except ExtractionError as exc:
            raise _HttpError(HTTPStatus.BAD_GATEWAY, f"LLM call failed: {exc}") from exc
        reply = str(result.get("content", ""))
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}

        response: dict[str, Any] = {
            "reply": reply,
            "session_id": session_id,
            "injected": injected,
            "inject_token_estimate": inject_tokens,
            "usage": usage,
            "peer_id": peer_id,
        }

        # auto_consolidate_every_n（默认 0 = 关）：每 N 轮自动巩固一次
        n = self.shell.auto_consolidate_every_n
        if n and n > 0 and self.extractor is not None and not self.read_only:
            key = (peer_id, session_id)
            count = self._chat_turn_counts[key] = self._chat_turn_counts.get(key, 0) + 1
            if count % n == 0:
                conversation = list(history) + [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": reply},
                ]
                try:
                    with self._lock:
                        report = self._brain(peer_id).consolidate(
                            conversation, session_id=session_id
                        )
                    response["auto_consolidate"] = report.to_dict()
                except Exception as exc:  # 巩固失败不阻塞聊天回复
                    response["auto_consolidate_error"] = str(exc)
        return response

    @staticmethod
    def _normalize_history(value: Any) -> list[Mapping[str, str]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "history must be an array")
        normalized: list[Mapping[str, str]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise _HttpError(HTTPStatus.BAD_REQUEST, "history items must be objects")
            role = _string(item.get("role"), "history[].role")
            if role not in ("user", "assistant"):
                raise _HttpError(HTTPStatus.BAD_REQUEST, "history[].role must be user|assistant")
            normalized.append({"role": role, "content": _string(item.get("content"), "history[].content")})
        return normalized

    def _now_of(self, body: Mapping[str, Any]):
        raw = body.get("now")
        if not raw:
            return None
        from .models import parse_iso8601

        return parse_iso8601(str(raw))

    def _inject(self, body: Mapping[str, Any]) -> dict[str, Any]:
        peer_id = _peer(body)
        query_text = _string(body.get("query"), "query")
        recent = _string_list(body.get("recent_context"), "recent_context")
        entities = _string_list(body.get("entities"), "entities")
        token_limit = body.get("token_limit")
        if token_limit is not None:
            token_limit = int(token_limit)
        now = _parse_now(body.get("now"))
        views = _string_list(body.get("views"), "views")

        with self._lock:
            injection = self._brain(peer_id).build_injection(
                query_text,
                recent_context=recent,
                entities=entities,
                token_limit=token_limit,
                now=now,
                views=views,
            )
        return {
            "peer_id": peer_id,
            "context": injection.context,
            "estimated_tokens": injection.estimated_tokens,
            "token_limit": injection.token_limit,
            "memory_ids": injection.retrieved.memory_ids,
        }

    def _consolidate(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.extractor is None or self.read_only:
            raise _HttpError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "read-only mode: no extractor configured (DEEPSEEK_API_KEY not set)",
            )
        peer_id = _peer(body)
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "messages must be a non-empty array")
        normalized: list[Mapping[str, str]] = [
            {"role": _string(item["role"], "messages[].role"), "content": _string(item["content"], "messages[].content")}
            for item in messages
        ]
        session_id = body.get("session_id")
        if session_id is not None:
            session_id = _string(session_id, "session_id")
        now = _parse_now(body.get("now"))

        with self._lock:
            report = self._brain(peer_id).consolidate(
                normalized, session_id=session_id, now=now
            )
        result = report.to_dict()
        result["peer_id"] = peer_id
        return result

    def _forget(self, body: Mapping[str, Any]) -> dict[str, Any]:
        peer_id = _peer(body)
        memory_id = _string(body.get("memory_id"), "memory_id")
        hard = bool(body.get("hard", False))
        approved = bool(body.get("approved", False))
        with self._lock:
            try:
                result = self._brain(peer_id).forget(
                    memory_id, hard=hard, approved=approved
                )
            except PermissionError as exc:
                raise _HttpError(HTTPStatus.FORBIDDEN, str(exc)) from exc
        result["peer_id"] = peer_id
        return result

    def _proactive(self, body: Mapping[str, Any]) -> dict[str, Any]:
        peer_id = _peer(body)
        force = bool(body.get("force", False))
        ensure = bool(body.get("ensure", False))
        local_hour = body.get("local_hour")
        if local_hour is not None:
            try:
                local_hour = int(local_hour)
            except (TypeError, ValueError):
                raise _HttpError(HTTPStatus.BAD_REQUEST, "local_hour must be an int 0-23") from None
            if not 0 <= local_hour <= 23:
                raise _HttpError(HTTPStatus.BAD_REQUEST, "local_hour must be 0-23")
        now = _parse_now(body.get("now"))
        with self._lock:
            brain = self._brain(peer_id)
            if not hasattr(brain, "build_proactive_prompt"):
                raise _HttpError(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "memory library too old for proactive endpoints; upgrade extra_brain",
                )
            if ensure:
                if self.read_only:
                    raise _HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "read-only mode")
                brain.ensure_proactive_config(peer_id)
            result = brain.build_proactive_prompt(
                peer_id, now=now, force=force, local_hour=local_hour
            )
        result["peer_id"] = peer_id
        return result

    def _proactive_record(self, body: Mapping[str, Any]) -> dict[str, Any]:
        peer_id = _peer(body)
        now = _parse_now(body.get("now"))
        umo = body.get("umo")
        if umo is not None:
            umo = _string(umo, "umo")
        with self._lock:
            brain = self._brain(peer_id)
            if not hasattr(brain, "record_proactive_open"):
                raise _HttpError(HTTPStatus.NOT_IMPLEMENTED, "memory library too old")
            if self.read_only:
                raise _HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "read-only mode")
            brain.record_proactive_open(peer_id, now=now, umo=umo)
        return {"peer_id": peer_id, "recorded": True}

    def _abstract_dispositions(self, body: Mapping[str, Any]) -> list[dict[str, Any]]:
        peer_id = _peer(body)
        limit = int(body.get("limit", 3))
        with self._lock:
            return self._brain(peer_id).abstract_dispositions(limit=limit)

    def _snapshot(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.read_only:
            raise _HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "read-only mode")
        peer_id = _peer(body)
        reason = _string(body.get("reason"), "reason") if body.get("reason") else ""
        with self._lock:
            sid = self._brain(peer_id).take_snapshot(reason=reason)
        return {"peer_id": peer_id, "snapshot_id": sid}

    def _snapshot_rollback(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.read_only:
            raise _HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "read-only mode")
        peer_id = _peer(body)
        snapshot_id = _string(body.get("snapshot_id"), "snapshot_id")
        with self._lock:
            state = self._brain(peer_id).rollback_to_snapshot(snapshot_id)
        if state is None:
            raise _HttpError(HTTPStatus.NOT_FOUND, f"snapshot {snapshot_id} not found")
        return state.to_dict()

    def _list(self, peer_id: str, kind: str) -> dict[str, Any]:
        with self._lock:
            brain = self._brain(peer_id)
            if kind == "episodic":
                items: Sequence[Any] = brain.store.list_episodic()
            elif kind == "style":
                items = brain.store.list_style()
            else:
                items = brain.store.list_semantic()
        return {"peer_id": peer_id, "kind": kind, "items": [item.to_dict() for item in items]}

    def close(self) -> None:
        with self._lock:
            for brain in self._brains.values():
                brain.close()
            self._brains.clear()
            for brain in self._preview_brains.values():
                brain.close()
            self._preview_brains.clear()


class _Handler(BaseHTTPRequestHandler):
    server: "ThreadingHTTPServer"

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route in ("/", "/shell", "/shell/"):
            self._serve_shell_index()
            return
        self._dispatch("GET", {})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
            return
        if not isinstance(body, dict):
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object"})
            return
        self._dispatch("POST", body)

    def _serve_shell_index(self) -> None:
        index = _SHELL_STATIC_DIR / "index.html"
        if not index.is_file():
            self._respond_bytes(
                HTTPStatus.NOT_FOUND,
                "text/html; charset=utf-8",
                "<h1>shell UI not found</h1><p>missing src/extra_brain/static/shell/index.html</p>".encode("utf-8"),
            )
            return
        try:
            data = index.read_bytes()
        except OSError:
            self._respond_bytes(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain", b"cannot read shell UI")
            return
        self._respond_bytes(HTTPStatus.OK, "text/html; charset=utf-8", data)

    def _dispatch(self, method: str, body: Mapping[str, Any]) -> None:
        try:
            result = self.server.app.handle(method, self.path, body, headers=self.headers)
        except _HttpError as exc:
            self._respond(exc.status, {"error": exc.message})
        except (MemoryValidationError, ValueError, TypeError, KeyError) as exc:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except ExtractionError as exc:
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - last-resort guard
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})
        else:
            self._respond(HTTPStatus.OK, result)

    def _respond(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond_bytes(self, status: HTTPStatus, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep stdout clean; wire to a logger if the host needs access logs


class _HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _peer(body: Mapping[str, Any]) -> str:
    return _string(body.get("peer_id"), "peer_id")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _HttpError(HTTPStatus.BAD_REQUEST, f"{name} must be a non-empty string")
    return value


def _string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _HttpError(HTTPStatus.BAD_REQUEST, f"{name} must be an array of strings")
    return value


def _as_bool(value: Any, default: bool) -> bool:
    """宽松布尔解析：环境变量/JSON 里的 "0"/"false"/"off" 都算 False。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "none", "")
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_now(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _HttpError(HTTPStatus.BAD_REQUEST, "now must be an ISO8601 string or null")
    try:
        return parse_iso8601(value)
    except MemoryValidationError as exc:
        raise _HttpError(HTTPStatus.BAD_REQUEST, f"invalid now: {exc}") from exc


def make_server(
    data_dir: str | Path,
    *,
    extractor: MemoryExtractor | None = None,
    host: str = "127.0.0.1",
    port: int = 8756,
    config: MemoryConfig | None = None,
    shell: ShellSettings | None = None,
    user_config: Mapping[str, Any] | None = None,
    user_config_path: str | Path | None = None,
    chat_client: Any = None,
) -> ThreadingHTTPServer:
    app = ExtraBrainServer(
        data_dir,
        extractor=extractor,
        config=config,
        shell=shell,
        user_config=user_config,
        user_config_path=user_config_path,
        chat_client=chat_client,
    )
    server = ThreadingHTTPServer((host, port), _Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def _load_user_config_file(path: str | Path | None) -> tuple[dict[str, Any], Path | None]:
    """读取 user_config.json（人设/安全规范/shell 配置）。不存在返回空 dict。"""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    env_path = os.environ.get("EXTRA_BRAIN_USER_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("config/user_config.json"))
    for candidate in candidates:
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}, candidate
            return (data if isinstance(data, dict) else {}), candidate
    return {}, None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="extra_brain 本地 HTTP sidecar + Chat Shell")
    parser.add_argument("--data-dir", default="extra-brain")
    parser.add_argument("--host", default="127.0.0.1", help="默认仅本机；安全上禁止默认对外开放")
    parser.add_argument("--port", type=int, default=8756)
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="只读模式：拒绝 /consolidate 与 /forget 写入（v0.3.0+）",
    )
    parser.add_argument(
        "--encryption-passphrase",
        default=None,
        help="静态加密口令（v0.5.0）；默认读取 EXTRA_BRAIN_ENCRYPTION_PASSPHRASE 环境变量",
    )
    parser.add_argument(
        "--user-config",
        default=None,
        help="user_config.json 路径（人设/安全规范/shell 配置）；默认自动查找 config/user_config.json",
    )
    parser.add_argument(
        "--no-shell",
        action="store_true",
        help="关闭 v0.13.0 /v1 shell API（仅保留旧 sidecar 端点）",
    )
    args = parser.parse_args(argv)

    user_config, config_path = _load_user_config_file(args.user_config)
    shell = ShellSettings(
        enabled=(False if args.no_shell else None),
        user_config=user_config,
    )
    if shell.enabled and not shell.token:
        shell.token = secrets.token_hex(16)
        print(f"shell token (generated, save it): {shell.token}")

    app = ExtraBrainServer(
        args.data_dir,
        extractor=None if args.read_only else _default_extractor(),
        read_only=args.read_only,
        encryption_passphrase=args.encryption_passphrase,
        shell=shell,
        user_config=user_config,
        user_config_path=config_path,
    )
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    server.app = app  # type: ignore[attr-defined]
    mode = (
        "read-only (explicit)"
        if args.read_only
        else "read-write"
        if app.extractor is not None
        else "read-only (no DEEPSEEK_API_KEY)"
    )
    shell_state = (
        f"shell on (peer {shell.peer_id}, token {shell.token if shell.token else 'MISSING — shell API will 401'})"
        if shell.enabled
        else "shell off"
    )
    print(f"extra_brain sidecar listening on http://{args.host}:{args.port} [{mode}] [{shell_state}]")
    if shell.enabled:
        print(f"chat shell UI: http://{args.host}:{args.port}/shell/")
        policy = shell.auto_consolidate_policy()
        print(
            "auto consolidate: "
            + (
                f"on (every {policy['every_n_user_messages']} user msgs, "
                f"min {policy['min_messages']}, "
                f"flush on new session={'yes' if policy['on_new_session'] else 'no'}, "
                f"on page hide={'yes' if policy['on_page_hide'] else 'no'})"
                if policy["enabled"]
                else "off"
            )
        )
        if shell.token:
            print("打开网页后如提示 Token 未设置，把上面日志里的 token 粘贴进去即可（只需一次）")
    print(f"data dir: {Path(args.data_dir).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


def _default_extractor() -> MemoryExtractor | None:
    try:
        return OpenAICompatibleExtractor.from_env()
    except ExtractionError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
