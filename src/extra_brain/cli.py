from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import MemoryConfig
from .engine import ExtraBrain
from .extraction import ExtractionError, OpenAICompatibleExtractor
from .models import CoreMemory, MemoryValidationError
from .session import SessionTracker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="extra-brain", description="可审计的长期记忆系统")
    parser.add_argument(
        "--db",
        default=os.environ.get("EXTRA_BRAIN_DB", "extra_brain.db"),
        help="SQLite 数据库路径（默认：./extra_brain.db）",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="初始化数据库并显示 Core Memory")

    core = commands.add_parser("core", help="读取 Core，或从 JSON 文件完整替换 Core")
    core.add_argument("--file", type=Path, help="符合 Core schema 的 JSON 文件")

    consolidate = commands.add_parser("consolidate", help="使用 DeepSeek 深度抽取并巩固对话")
    consolidate.add_argument("file", type=Path, help="UTF-8 对话文本文件")
    consolidate.add_argument("--session-id", help="写入情节记忆的 raw_reference")

    search = commands.add_parser("search", help="检索记忆并输出各项评分")
    search.add_argument("query")
    search.add_argument("--entity", action="append", default=[], help="实体/关键词硬过滤，可重复")
    search.add_argument("--context-file", type=Path, help="最近上下文，每行一轮")

    inject = commands.add_parser("inject", help="生成可直接放到系统提示词中的长期记忆上下文")
    inject.add_argument("query")
    inject.add_argument("--entity", action="append", default=[])
    inject.add_argument("--context-file", type=Path, help="最近上下文，每行一轮")
    inject.add_argument("--token-limit", type=int)

    list_command = commands.add_parser("list", help="列出长期记忆")
    list_command.add_argument("kind", choices=("semantic", "episodic", "style"))
    list_command.add_argument("--include-archived", action="store_true")

    protect = commands.add_parser("protect", help="将记忆加入/移出 Core 保护列表")
    protect.add_argument("memory_id")
    protect.add_argument("--remove", action="store_true")

    audit = commands.add_parser("audit", help="查看操作审计与 before/after 快照")
    audit.add_argument("--limit", type=int, default=50)

    commands.add_parser("reindex", help="用当前 embedding 实现重建所有向量")

    chat = commands.add_parser("chat", help="本地交互式对话，自动/半自动巩固记忆")
    chat.add_argument("--threshold", type=int, default=8, help="累计多少轮后自动巩固（默认 8）")
    chat.add_argument("--no-auto", action="store_true", help="关闭自动巩固，仅手动 :consolidate")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    brain: ExtraBrain | None = None
    try:
        if args.command in ("consolidate", "chat"):
            brain = ExtraBrain(
                args.db,
                extractor=OpenAICompatibleExtractor.from_env(),
                config=MemoryConfig(),
            )
        else:
            brain = ExtraBrain(args.db)

        if args.command == "init":
            _print_json(brain.core.to_dict())
        elif args.command == "core":
            if args.file:
                data = json.loads(args.file.read_text(encoding="utf-8"))
                brain.save_core(CoreMemory.from_dict(data))
            _print_json(brain.core.to_dict())
        elif args.command == "consolidate":
            conversation = args.file.read_text(encoding="utf-8")
            report = brain.consolidate(conversation, session_id=args.session_id)
            _print_json(report.to_dict())
        elif args.command in ("search", "inject"):
            recent = (
                args.context_file.read_text(encoding="utf-8").splitlines()[-5:]
                if getattr(args, "context_file", None)
                else []
            )
            if args.command == "search":
                result = brain.retrieve(args.query, recent_context=recent, entities=args.entity)
                _print_json(result.to_dict())
            else:
                injection = brain.build_injection(
                    args.query,
                    recent_context=recent,
                    entities=args.entity,
                    token_limit=args.token_limit,
                )
                print(injection.context)
        elif args.command == "list":
            if args.kind == "semantic":
                memories: list[Any] = brain.store.list_semantic(
                    include_archived=args.include_archived
                )
            elif args.kind == "episodic":
                memories = brain.store.list_episodic(include_archived=args.include_archived)
            else:
                memories = brain.store.list_style()
            _print_json([memory.to_dict() for memory in memories])
        elif args.command == "protect":
            brain.store.set_protected(args.memory_id, not args.remove)
            _print_json(brain.core.to_dict())
        elif args.command == "audit":
            _print_json(brain.store.list_audit(limit=args.limit))
        elif args.command == "reindex":
            _print_json({"indexed": brain.reindex()})
        elif args.command == "chat":
            return _run_chat(brain, args.threshold, auto=not args.no_auto)
        return 0
    except (ExtractionError, MemoryValidationError, OSError, KeyError, ValueError) as exc:
        print(f"extra-brain: {exc}", file=sys.stderr)
        return 1
    finally:
        if brain is not None:
            brain.close()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _run_chat(brain: ExtraBrain, threshold: int, *, auto: bool) -> int:
    _configure_utf8()
    tracker = SessionTracker(brain, consolidate_every=threshold)
    session_id = tracker.start_session()
    _print_text(f"会话已开始：{session_id}")
    _print_text("每行是一条 user 消息；以 'a: ' 开头记为 assistant 消息。")
    _print_text(
        "命令：:inject <查询>  :consolidate  :core  :list <semantic|episodic|style>"
        "  :new  :help  :quit"
    )
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = ":quit"
        if not line:
            continue

        lowered = line.lower()
        if lowered in (":quit", ":q", "/quit", "/q", "exit"):
            report = tracker.consolidate()
            if report is not None:
                _print_report_summary(report)
            _print_text("已退出。")
            break
        if lowered in (":help", "/help"):
            _print_chat_help()
            continue
        if lowered.startswith((":inject", ":remember")):
            query = _after_colon(line)
            if not query:
                query = input("inject query> ").strip()
            if not query:
                _print_text("（查询为空）")
                continue
            injection = tracker.inject(query)
            _print_text("\n----- 注入上下文 -----")
            _print_text(injection.context)
            _print_text(
                f"\n({injection.estimated_tokens} tokens / 预算 {injection.token_limit})"
            )
            continue
        if lowered.startswith(":consolidate"):
            _print_report_summary(tracker.consolidate())
            continue
        if lowered.startswith(":core"):
            _print_json(brain.core.to_dict())
            continue
        if lowered.startswith(":list"):
            _print_list(brain, _after_colon(line) or "semantic")
            continue
        if lowered in (":new", "/new"):
            report = tracker.consolidate()
            if report is not None:
                _print_report_summary(report)
            session_id = tracker.start_session()
            _print_text(f"新会话已开始：{session_id}")
            continue

        role, content = _parse_turn(line)
        report = tracker.add(role, content, auto_consolidate=auto)
        if report is not None:
            _print_report_summary(report)
    return 0


def _after_colon(line: str) -> str:
    if ":" not in line:
        return ""
    return line.split(":", 1)[1].strip()


def _parse_turn(line: str) -> tuple[str, str]:
    lowered = line.lower()
    if lowered.startswith("a:"):
        return "assistant", line.split(":", 1)[1].strip()
    if lowered.startswith("u:"):
        return "user", line.split(":", 1)[1].strip()
    return "user", line


def _print_report_summary(report: Any) -> None:
    if report is None:
        _print_text("（没有待巩固的消息）")
        return
    _print_text(
        f"已巩固：语义 {report.semantic_written}，情节 {report.episodic_written}，"
        f"风格 {report.style_written}，归档 {report.archived}，"
        f"冲突 {len(report.conflicts)}，清除 Core {len(report.core_pruned)}"
    )


def _print_list(brain: ExtraBrain, kind: str) -> None:
    kind = kind.strip().lower()
    if kind == "episodic":
        items: list[Any] = brain.store.list_episodic()
        for item in items:
            _print_text(f"[{item.id}] {item.timestamp} {item.summary}")
    elif kind == "style":
        items = brain.store.list_style()
        for item in items:
            _print_text(f"[{item.id}] {item.category} {item.observation}")
    else:
        items = brain.store.list_semantic()
        for item in items:
            _print_text(
                f"[{item.id}] {item.type} {item.subject}/{item.predicate}/{item.object} "
                f"conf={item.confidence:.2f} conflict={item.conflict_flag}"
            )


def _print_chat_help() -> None:
    _print_text(
        "输入规则：普通行=user；'a: xxx'=assistant 轮。\n"
        ":inject <查询>   查看当前查询会注入哪些长期记忆（含完整 Core）\n"
        ":consolidate     立即巩固当前未处理消息\n"
        ":core            查看 Core Memory\n"
        ":list <kind>     列出 semantic / episodic / style\n"
        ":new             结束当前会话并开启新会话（自动巩固剩余消息）\n"
        ":quit            退出（自动巩固剩余消息）"
    )


def _configure_utf8() -> None:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _print_text(value: str) -> None:
    print(value)


if __name__ == "__main__":
    raise SystemExit(main())
