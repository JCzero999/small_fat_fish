"""Local HTTP sidecar that exposes ExtraBrain to a non-Python host (e.g. DSH/Cordis).

The host plugin is TypeScript; this server is the language bridge. Each peer gets
its own SQLite file under ``<data_dir>/<peer_id>.db`` so private chats and group
chats stay isolated.

Endpoints
---------
GET  /health                      -> {"ok": true, "peers": <count>}
POST /inject                      -> build_injection(...) as JSON
POST /consolidate                 -> consolidate(messages, session_id) report
GET  /core?peer_id=...            -> CoreMemory as JSON (debug/inspection)
GET  /list?peer_id=...&kind=...   -> memory list (debug/inspection)

Run
---
    export DEEPSEEK_API_KEY="..."
    python -m extra_brain.server --data-dir ./extra-brain --port 8756

Without ``DEEPSEEK_API_KEY`` the server still starts, but ``/consolidate`` returns
503 (read-only mode); ``/inject``, ``/core``, ``/list`` keep working.
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

from .config import MemoryConfig
from .engine import ExtraBrain
from .extraction import ExtractionError, MemoryExtractor, OpenAICompatibleExtractor
from .models import MemoryValidationError, parse_iso8601


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
    ) -> None:
        self.data_dir = Path(data_dir)
        self.config = config or MemoryConfig()
        self.extractor = extractor
        self.read_only = bool(read_only)
        self.encryption_passphrase = encryption_passphrase
        self._brains: dict[str, ExtraBrain] = {}
        self._lock = threading.RLock()

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
                    self.data_dir / f"{peer_id}.db",
                    extractor=self.extractor,
                    config=self.config,
                    read_only=self.read_only,
                    encryption_passphrase=self.encryption_passphrase,
                )
                self._brains[peer_id] = brain
            return brain

    @property
    def peer_count(self) -> int:
        with self._lock:
            return len(self._brains)

    def handle(self, method: str, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        parsed = urlparse(path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        route = parsed.path.rstrip("/") or "/"

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

        raise _HttpError(HTTPStatus.NOT_FOUND, f"unknown route {method} {route}")

    def _inject(self, body: Mapping[str, Any]) -> dict[str, Any]:
        peer_id = _peer(body)
        query_text = _string(body.get("query"), "query")
        recent = _string_list(body.get("recent_context"), "recent_context")
        entities = _string_list(body.get("entities"), "entities")
        token_limit = body.get("token_limit")
        if token_limit is not None:
            token_limit = int(token_limit)
        now = _parse_now(body.get("now"))

        with self._lock:
            injection = self._brain(peer_id).build_injection(
                query_text,
                recent_context=recent,
                entities=entities,
                token_limit=token_limit,
                now=now,
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


class _Handler(BaseHTTPRequestHandler):
    server: "ThreadingHTTPServer"

    def do_GET(self) -> None:
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

    def _dispatch(self, method: str, body: Mapping[str, Any]) -> None:
        try:
            result = self.server.app.handle(method, self.path, body)
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
) -> ThreadingHTTPServer:
    app = ExtraBrainServer(data_dir, extractor=extractor, config=config)
    server = ThreadingHTTPServer((host, port), _Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="extra_brain 本地 HTTP sidecar")
    parser.add_argument("--data-dir", default="extra-brain")
    parser.add_argument("--host", default="127.0.0.1")
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
    args = parser.parse_args(argv)

    app = ExtraBrainServer(
        args.data_dir,
        extractor=None if args.read_only else _default_extractor(),
        read_only=args.read_only,
        encryption_passphrase=args.encryption_passphrase,
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
    print(f"extra_brain sidecar listening on http://{args.host}:{args.port} [{mode}]")
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
