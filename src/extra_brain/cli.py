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
from .models import CoreMemory, MEMORY_VIEWS, MemoryValidationError, to_iso8601, utc_now
from .session import SessionTracker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="extra-brain", description="可审计的长期记忆系统")
    parser.add_argument(
        "--db",
        default=os.environ.get("EXTRA_BRAIN_DB", "extra_brain.db"),
        help="SQLite 数据库路径（默认：./extra_brain.db）",
    )
    parser.add_argument(
        "--companion-db",
        default=os.environ.get("EXTRA_BRAIN_COMPANION_DB", ""),
        help="全局伴侣库路径（人设/称呼/稳定偏好跨场景共享；默认 EXTRA_BRAIN_COMPANION_DB）",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="初始化数据库并显示 Core Memory")

    core = commands.add_parser("core", help="读取 Core，或从 JSON 文件完整替换 Core")
    core.add_argument("--file", type=Path, help="符合 Core schema 的 JSON 文件")
    core.add_argument(
        "--rollback",
        metavar="REVISION",
        help="将人格锚回滚到指定版本（REVISION 为 persona_anchor_history 中的版本 ID）",
    )

    consolidate = commands.add_parser("consolidate", help="使用 DeepSeek 深度抽取并巩固对话")
    consolidate.add_argument("file", type=Path, help="UTF-8 对话文本文件")
    consolidate.add_argument("--session-id", help="写入情节记忆的 raw_reference")

    search = commands.add_parser("search", help="检索记忆并输出各项评分")
    search.add_argument("query")
    search.add_argument("--entity", action="append", default=[], help="实体/关键词硬过滤，可重复")
    search.add_argument("--context-file", type=Path, help="最近上下文，每行一轮")
    search.add_argument(
        "--view", action="append", default=[], choices=sorted(MEMORY_VIEWS),
        help="按视角过滤（fact/feeling/bond/story，可重复）",
    )

    inject = commands.add_parser("inject", help="生成可直接放到系统提示词中的长期记忆上下文")
    inject.add_argument("query")
    inject.add_argument("--entity", action="append", default=[])
    inject.add_argument("--context-file", type=Path, help="最近上下文，每行一轮")
    inject.add_argument("--token-limit", type=int)
    inject.add_argument(
        "--view", action="append", default=[], choices=sorted(MEMORY_VIEWS),
        help="按视角过滤（fact/feeling/bond/story，可重复）",
    )

    list_command = commands.add_parser("list", help="列出长期记忆")
    list_command.add_argument("kind", choices=("semantic", "episodic", "style"))
    list_command.add_argument("--include-archived", action="store_true")

    protect = commands.add_parser("protect", help="将记忆加入/移出 Core 保护列表")
    protect.add_argument("memory_id")
    protect.add_argument("--remove", action="store_true")

    # v0.5.0 遗忘链路：默认归档+墓碑（P1），物理删除（P0）需 --hard --yes 显式审批
    forget = commands.add_parser("forget", help="遗忘记忆（默认 P1 墓碑化；--hard --yes 才物理删除）")
    forget.add_argument("memory_id")
    forget.add_argument("--hard", action="store_true", help="物理删除（P0），需配合 --yes")
    forget.add_argument("--yes", action="store_true", help="显式审批物理删除")

    # v0.5.0 存储分层统计
    commands.add_parser("stats", help="查看记忆库分层统计（保留度档位/归档比例）")

    # v0.5.0 记忆联想：打印 2 跳联想链
    associate = commands.add_parser("associate", help="查看某条记忆的 2 跳联想链")
    associate.add_argument("memory_id")

    audit = commands.add_parser("audit", help="查看操作审计与 before/after 快照")
    audit.add_argument("--limit", type=int, default=50)

    commands.add_parser("reindex", help="用当前 embedding 实现重建所有向量")

    rebuild = commands.add_parser(
        "rebuild", help="崩溃安全重建：--from-log 从审计快照重放记忆表；replay-pending 重放未巩固输入"
    )
    rebuild.add_argument(
        "mode", choices=("from-log", "replay-pending"), help="from-log=重建派生层；replay-pending=重放影子输入"
    )
    rebuild.add_argument("--yes", action="store_true", help="from-log 全表重建需显式审批")
    rebuild.add_argument("--limit", type=int, default=10, help="replay-pending 最多重放条数")

    chat = commands.add_parser("chat", help="本地交互式对话，自动/半自动巩固记忆")
    chat.add_argument("--threshold", type=int, default=8, help="累计多少轮后自动巩固（默认 8）")
    chat.add_argument("--no-auto", action="store_true", help="关闭自动巩固，仅手动 :consolidate")

    commands.add_parser("sync-persona", help="把本库人设/称呼/稳定偏好推入全局伴侣库（--companion-db）")
    commands.add_parser("pull-persona", help="从全局伴侣库拉取人设到本库（新场景自动继承）")

    # v0.7.0 / v0.9.0 SelfState
    self_cmd = commands.add_parser("self", help="查看当前自我状态（能量/心情/目标/疲劳/叙事）")
    self_cmd.add_argument("--rollback", type=int, metavar="VERSION",
                          help="回滚到指定版本（暂未实现，先做查看）")

    # v0.8.0 DispositionSeed
    list_disp = commands.add_parser("list-dispositions",
                                    help="列出所有倾向种子")
    abstract_cmd = commands.add_parser("abstract-dispositions",
                                       help="从记忆中抽象新的倾向种子")
    abstract_cmd.add_argument("--limit", type=int, default=3, help="最多生成几条（默认 3）")

    # v0.9.0 life snapshots
    snapshot = commands.add_parser("snapshot", help="保存当前自我状态为人生快照")
    snapshot.add_argument("--reason", default="", help="快照原因")

    commands.add_parser("snapshots", help="列出所有人生快照")

    snap_rollback = commands.add_parser("snapshot-rollback",
                                        help="从快照恢复 SelfState")
    snap_rollback.add_argument("snapshot_id", help="快照 ID")

    # v0.11.0 A3 影子模式
    shadow_cmd = commands.add_parser("shadow",
                                     help="影子模式：status|on|off（SelfState/倾向记录但不注入）")
    shadow_cmd.add_argument("action", nargs="?", default="status",
                            choices=["status", "on", "off"], help="默认 status")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    companion = args.companion_db or None
    brain: ExtraBrain | None = None
    try:
        if args.command in ("consolidate", "chat") or (
            args.command == "rebuild" and args.mode == "replay-pending"
        ):
            brain = ExtraBrain(
                args.db,
                extractor=OpenAICompatibleExtractor.from_env(),
                config=MemoryConfig(),
                companion_db=companion,
            )
        else:
            brain = ExtraBrain(args.db, companion_db=companion)

        if args.command == "init":
            _print_json(brain.core.to_dict())
        elif args.command == "core":
            if args.file:
                data = json.loads(args.file.read_text(encoding="utf-8"))
                brain.save_core(CoreMemory.from_dict(data))
            elif args.rollback:
                core = brain.core
                revision = args.rollback
                history = list(core.persona_anchor_history)
                matches = [h for h in history if h.get("version") == revision]
                if not matches:
                    print(
                        f"extra-brain: persona_anchor_history 中未找到版本 {revision!r}",
                        file=sys.stderr,
                    )
                    return 1
                entry = matches[-1]
                core.persona_anchor_version = entry.get("version")
                core.persona_anchor_history = history
                core.last_updated = to_iso8601(utc_now())
                brain.save_core(core)
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
                result = brain.retrieve(
                    args.query, recent_context=recent, entities=args.entity,
                    views=getattr(args, "view", []),
                )
                _print_json(result.to_dict())
            else:
                injection = brain.build_injection(
                    args.query,
                    recent_context=recent,
                    entities=args.entity,
                    token_limit=args.token_limit,
                    views=getattr(args, "view", []),
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
        elif args.command == "forget":
            result = brain.forget(
                args.memory_id, hard=args.hard, approved=args.yes
            )
            _print_json(result)
        elif args.command == "stats":
            _print_json(brain.storage_stats())
        elif args.command == "associate":
            _print_json(_association_chain(brain, args.memory_id))
        elif args.command == "audit":
            _print_json(brain.store.list_audit(limit=args.limit))
        elif args.command == "reindex":
            _print_json({"indexed": brain.reindex()})
        elif args.command == "sync-persona":
            _print_json(brain.sync_persona_to_companion())
        elif args.command == "pull-persona":
            _print_json(brain.pull_persona_from_companion())
        elif args.command == "self":
            state = brain.get_self_state()
            data = state.to_dict()
            from .self_state import count_unresolved, energy_tier
            data["energy_tier"] = energy_tier(state)
            data["unresolved_count"] = count_unresolved(brain.store)
            _print_json(data)
        elif args.command == "list-dispositions":
            _print_json(brain.list_dispositions())
        elif args.command == "abstract-dispositions":
            _print_json(brain.abstract_dispositions(limit=args.limit))
        elif args.command == "snapshot":
            _print_json({"snapshot_id": brain.take_snapshot(reason=args.reason)})
        elif args.command == "snapshots":
            _print_json(brain.list_snapshots())
        elif args.command == "snapshot-rollback":
            result = brain.rollback_to_snapshot(args.snapshot_id)
            if result is None:
                print(f"extra-brain: snapshot {args.snapshot_id} not found", file=sys.stderr)
                return 1
            _print_json(result)
        elif args.command == "shadow":
            if args.action == "status":
                _print_json({"shadow_mode": brain.shadow_mode})
            else:
                _print_json({"shadow_mode": brain.set_shadow_mode(args.action == "on")})
        elif args.command == "rebuild":
            if args.mode == "from-log":
                if not args.yes:
                    print(
                        "extra-brain: rebuild from-log 会用审计快照整表重建（先建议备份），"
                        "确认请加 --yes",
                        file=sys.stderr,
                    )
                    return 1
                _print_json(brain.rebuild_from_log())
            else:
                _print_json(brain.replay_pending(limit=args.limit))
        elif args.command == "chat":
            return _run_chat(brain, args.threshold, auto=not args.no_auto)
        return 0
    except (ExtractionError, MemoryValidationError, OSError, KeyError, ValueError, PermissionError) as exc:
        print(f"extra-brain: {exc}", file=sys.stderr)
        return 1
    finally:
        if brain is not None:
            brain.close()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _association_chain(brain: ExtraBrain, memory_id: str, *, max_hops: int = 2) -> dict[str, Any]:
    """v0.5.0 /memory associate：从一条记忆出发打印 2 跳联想链。"""
    memory = brain.store.get_memory(memory_id)
    if memory is None:
        raise KeyError(memory_id)
    seen = {memory_id}
    frontier = [memory_id]
    hops: list[list[dict[str, Any]]] = []
    for _ in range(max_hops):
        next_frontier: list[str] = []
        layer: list[dict[str, Any]] = []
        for current in frontier:
            for association in brain.list_associations_for_memory(current):
                neighbor_id = (
                    association.target_memory_id
                    if association.source_memory_id == current
                    else association.source_memory_id
                )
                if neighbor_id in seen:
                    continue
                seen.add(neighbor_id)
                neighbor = brain.store.get_memory(neighbor_id)
                layer.append(
                    {
                        "id": neighbor_id,
                        "strength": association.strength,
                        "digest": neighbor.embedding_text()[:80] if neighbor else "",
                    }
                )
                next_frontier.append(neighbor_id)
        if not layer:
            break
        layer.sort(key=lambda item: item["strength"], reverse=True)
        hops.append(layer)
        frontier = next_frontier
    return {"from": memory_id, "hops": hops}


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
