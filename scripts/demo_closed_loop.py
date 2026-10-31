"""Local closed-loop demo: 3 simulated sessions across 3 days.

Runs the real DeepSeek extractor (DEEPSEEK_API_KEY required) against a scripted
companion conversation that includes an emotional node, a correction, and
irrelevant chatter, then dumps the memory state and injection contexts to JSON.

Usage:
    export DEEPSEEK_API_KEY=...
    python scripts/demo_closed_loop.py --db /tmp/demo.db --out /tmp/demo_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from extra_brain import ExtraBrain, SessionTracker  # noqa: E402
from extra_brain.retrieval import Injection  # noqa: E402

DAY_1 = datetime(2026, 10, 28, 20, 30, tzinfo=timezone.utc)
DAY_2 = datetime(2026, 10, 29, 21, 15, tzinfo=timezone.utc)
DAY_3 = datetime(2026, 10, 30, 10, 5, tzinfo=timezone.utc)

SESSIONS = [
    {
        "session_id": "day-1-first-meeting",
        "now": DAY_1,
        "turns": [
            ("user", "你好呀，第一次用这个，我有点不知道聊什么。"),
            ("assistant", "没关系，想到什么说什么，我会认真听。"),
            ("user", "我最近在准备一个算法竞赛，压力挺大的。"),
            ("assistant", "竞赛准备确实容易紧绷，我陪你。"),
            ("user", "其实我最怕的是面试环节，之前一次被问懵了，一直有阴影。"),
            ("assistant", "那个阴影不代表你的能力，只是当时没准备好。"),
            ("user", "谢谢你这么说。以后叫我小舟吧。"),
            ("assistant", "好，小舟。我会记住。"),
            ("user", "对了，我平时靠喝白茶来放松，已经成习惯了，你帮我记住。"),
            ("assistant", "好，我记住了，喝白茶放松。"),
            ("user", "今天天气不错，我中午吃了碗牛肉面。"),
            ("assistant", "那挺好，忙里偷闲。"),
        ],
    },
    {
        "session_id": "day-2-check-in",
        "now": DAY_2,
        "start_query": "我又来了，还是担心面试。",
        "turns": [
            ("user", "我又来了。昨天的竞赛准备还是有点没底。"),
            ("assistant", "我在。先从你最担心的地方开始？"),
            ("user", "还是面试。我一想到被问懵那次就发慌。"),
            ("assistant", "那次已经过去了，你现在是有准备的人。"),
            ("user", "对了，我现在不喝白茶了，改喝绿茶，白茶太淡了。"),
            ("assistant", "记下了，绿茶。"),
            ("user", "今天地铁特别挤，还下了一天的雨。"),
        ],
    },
    {
        "session_id": "day-3-before-interview",
        "now": DAY_3,
        "start_query": "你还记得我上次说最怕什么吗？",
        "turns": [
            ("user", "早，我又想找你聊聊。"),
            ("assistant", "早，小舟。"),
            ("user", "你还记得我上次说最怕什么吗？"),
            ("assistant", "记得，面试环节。"),
            ("user", "我下周就要正式面试了，有点紧张，但比之前好多了。"),
            ("assistant", "你现在比之前更有底气了。"),
            ("user", "帮我记一下，下周三上午十点我要面试。"),
        ],
    },
]

FINAL_RECALL_QUERY = "你还记得我上次说最怕什么吗？"


def _injection_summary(injection: Injection) -> dict[str, Any]:
    return {
        "memory_ids": injection.retrieved.memory_ids,
        "estimated_tokens": injection.estimated_tokens,
        "token_limit": injection.token_limit,
        "context": injection.context,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="跨天本地闭环演示")
    parser.add_argument("--db", default="demo.db")
    parser.add_argument("--out", default="demo_report.json")
    args = parser.parse_args()

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(f"{args.db}{suffix}")
        except FileNotFoundError:
            pass

    report: dict[str, Any] = {"sessions": [], "session_start_injections": [], "final": {}}

    with ExtraBrain.from_env(args.db) as brain:
        tracker = SessionTracker(brain, consolidate_every=1000)  # 手动巩固，便于分“天”

        for session in SESSIONS:
            now: datetime = session["now"]
            tracker.start_session(session["session_id"])

            entry: dict[str, Any] = {
                "session_id": session["session_id"],
                "now": now.isoformat(),
                "conversation": session["turns"],
            }
            if "start_query" in session:
                entry["start_injection"] = _injection_summary(
                    tracker.inject(session["start_query"], now=now)
                )

            for role, content in session["turns"]:
                tracker.add(role, content, auto_consolidate=False)
            entry["consolidate"] = tracker.consolidate(now=now).to_dict()
            report["sessions"].append(entry)

        final_injection = tracker.inject(FINAL_RECALL_QUERY, now=DAY_3)
        report["final"] = {
            "query": FINAL_RECALL_QUERY,
            "injection": _injection_summary(final_injection),
            "core": brain.core.to_dict(),
            "semantic": [m.to_dict() for m in brain.store.list_semantic()],
            "episodic": [m.to_dict() for m in brain.store.list_episodic()],
            "style": [m.to_dict() for m in brain.store.list_style()],
        }

    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
