"""v0.8.0 DispositionSeed — 「种子 → 现行 → 再熏习」闭环。

- ``match_dispositions(query, store)`` — 匹配当前上下文与已有种子，返回 boost 记忆 ID + 内在声音短句
- ``reinforce_dispositions(store, report, conversation)`` — 从巩固报告检测认同/反对信号，调 strength
- ``abstract_dispositions(brain, limit)`` — 从记忆中抽象新种子（LLM 驱动，带规则过滤）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .models import (
    DispositionSeed,
    EpisodicMemory,
    SemanticMemory,
    new_memory_id,
    to_iso8601,
    utc_now,
)
from .storage import MemoryStore


@dataclass(slots=True)
class DispositionMatch:
    """匹配结果：打包需要注入和权重调整的信息。"""

    matched_seeds: list[DispositionSeed] = field(default_factory=list)
    boost_ids: list[str] = field(default_factory=list)  # evidence_ids to prime
    inner_voice: str = ""  # ≤40字 的内在声音
    boost_weight: float = 0.0  # 0.0–0.15


def match_dispositions(
    store: MemoryStore,
    query: str,
    *,
    recent_context: list[str] | None = None,
) -> DispositionMatch:
    """将当前消息与倾向种子做文本匹配。

    匹配方式：简单关键词重叠（含 statement 分词在 query/context 中的命中率）。
    高匹配种子 → 其 evidence 记忆 ID 纳入 boost，可选内在声音。

    返回 DispositionMatch，外部用它扩 primed_ids。
    """
    seeds = store.list_dispositions(include_archived=False)
    if not seeds:
        return DispositionMatch()

    context_text = " ".join(recent_context) if recent_context else ""
    combined = _normalize(query + " " + context_text)

    matched: list[DispositionSeed] = []
    boost_ids: list[str] = []
    best_statement = ""

    for seed in seeds:
        seed_text = _normalize(seed.statement)
        score = _text_match_score(seed_text, combined)
        # 激活过的种子再加一点 bias
        if seed.activation_count > 0:
            score += min(0.05, seed.activation_count * 0.01)
        if score < 0.15:
            continue
        matched.append(seed)
        for ev_id in seed.evidence_ids:
            if ev_id not in boost_ids:
                boost_ids.append(ev_id)
        if score >= 0.35:
            if not best_statement or seed.strength > 0:
                best_statement = seed.statement

    # 内在声音：取匹配度最高 + strength 最强的种子
    inner_voice = ""
    boost_weight = 0.0
    if matched:
        best = max(matched, key=lambda s: (s.strength, s.activation_count))
        inner_voice = _build_inner_voice(best.statement)
        boost_weight = min(0.15, 0.08 + best.strength * 0.07)

    return DispositionMatch(
        matched_seeds=matched,
        boost_ids=boost_ids,
        inner_voice=inner_voice,
        boost_weight=boost_weight,
    )


def reinforce_dispositions(
    store: MemoryStore,
    conversation: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """从对话文本中检测对已有种子的认同/反对信号，调整 strength。

    规则版（无需 LLM）：
    - 用户出现「是的/对/确实/就是这样」→ 强化 0.05
    - 用户出现「不对/不是/我不想/别」→ 削弱 0.08
    - 仅在对话题与种子 statement 有关联时才生效

    返回变更记录列表，供审计用。
    """
    now = now or utc_now()
    now_iso = to_iso8601(now)
    seeds = store.list_dispositions(include_archived=False)
    if not seeds or not conversation:
        return []

    conv_lower = conversation.lower()
    affirm = _count_keywords(conv_lower, _AFFIRM_KEYWORDS)
    deny = _count_keywords(conv_lower, _DENY_KEYWORDS)

    changes: list[dict[str, Any]] = []
    for seed in seeds:
        seed_text = _normalize(seed.statement)
        # 只有当种子和对话有实质关联时才生效
        if _text_match_score(seed_text, _normalize(conversation)) < 0.10:
            continue

        delta = 0.0
        reason = ""
        if affirm >= 2 and deny == 0:
            delta = 0.05
            reason = "affirm"
        elif deny >= 2:
            delta = -0.08
            reason = "deny"
        elif affirm >= 1:
            delta = 0.03
            reason = "weak_affirm"

        if delta != 0.0:
            new_strength = round(max(0.0, min(1.0, seed.strength + delta)), 4)
            seed.strength = new_strength
            seed.updated_at = now_iso
            if reason:
                seed.last_activated = now_iso
                seed.activation_count += 1
            store.save_disposition(seed)
            changes.append({
                "seed_id": seed.id,
                "statement": seed.statement[:40],
                "previous_strength": round(seed.strength - delta, 4),
                "new_strength": new_strength,
                "reason": reason,
            })

    # 归档：strength ≤ 0.15 的种子标记为 archived
    for seed in seeds:
        if seed.strength <= 0.15 and not seed.archived:
            store.archive_disposition(seed.id)
            changes.append({
                "seed_id": seed.id,
                "statement": seed.statement[:40],
                "reason": "archived_low_strength",
            })

    return changes


def abstract_dispositions(
    store: MemoryStore,
    limit: int = 3,
    *,
    extractor: Callable[[str], Any] | None = None,
    now: datetime | None = None,
) -> list[DispositionSeed]:
    """从记忆库中抽象新的倾向种子。

    - 规则版（无 extractor）：从高激活/高情绪/高频记忆中简单生成
    - LLM 版（有 extractor）：调模型提案，再经规则过滤

    去重：新种子与已有种子的 statement 文本相似度 > 0.6 → 合并
    """
    now = now or utc_now()
    now_iso = to_iso8601(now)

    existing = store.list_dispositions(include_archived=True)
    existing_statements = {_normalize(s.statement) for s in existing}

    if extractor is not None:
        return _abstract_via_llm(store, extractor, existing, existing_statements, limit, now, now_iso)

    # 规则版 fallback：从高 importance + 高 emotion_intensity 记忆中提取 pattern
    return _abstract_rule_based(store, existing_statements, limit, now, now_iso)


# ── helpers ────────────────────────────────────────────────────────


_AFFIRM_KEYWORDS = [
    "是的", "对", "确实", "就是这样", "没错", "说得对", "真的",
    "同意", "我也觉得", "对啊", "嗯嗯", "是的呢", "就是这样子",
]
_DENY_KEYWORDS = [
    "不对", "不是", "我不想", "别", "不要", "不行", "不可能",
    "才不", "并非", "不是这样", "你误会了", "不是的",
]


def _normalize(text: str) -> str:
    """简单规范化：去标点、去空格、小写。"""
    import re
    text = re.sub(r"[，。！？、；：""（）【】《》\s,.!?;:\"'()\[\]{}<>]+", "", text)
    return text.lower()


def _text_match_score(seed_text: str, context: str) -> float:
    """简单字符级重叠率，用于匹配判断。"""
    if not seed_text or not context:
        return 0.0
    seed_chars = set(seed_text)
    context_chars = set(context)
    if not seed_chars:
        return 0.0
    overlap = len(seed_chars & context_chars)
    return overlap / len(seed_chars)


def _count_keywords(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


def _build_inner_voice(statement: str) -> str:
    """从种子 statement 构建 ≤40 字的内在声音。"""
    # 简单截取 + "我倾向于..."
    short = statement[:35]
    if not short.endswith(("。", "！", "？", "…")):
        short = short.rstrip("，、；：,") + "…"
    return f"我倾向于{short}"[:40]


def _abstract_rule_based(
    store: MemoryStore,
    existing_statements: set[str],
    limit: int,
    now: datetime,
    now_iso: str,
) -> list[DispositionSeed]:
    """规则版抽象：从语义记忆中找高频 pattern。"""
    semantic = store.list_semantic(include_archived=False)
    episodic = store.list_episodic(include_archived=False)

    # 找到 repeated patterns：同一 subject 出现 ≥3 次
    subject_counts: dict[str, list[SemanticMemory]] = {}
    for mem in semantic:
        if mem.importance < 0.55:
            continue
        key = f"{mem.subject}|{mem.predicate}"
        subject_counts.setdefault(key, []).append(mem)

    results: list[DispositionSeed] = []
    for key, mems in subject_counts.items():
        if len(mems) < 3:
            continue
        subject, predicate = key.split("|", 1)
        # 构建 statement
        statement = f"面对{subject}时倾向于{predicate}"
        if len(statement) > 80:
            statement = statement[:77] + "..."
        norm = _normalize(statement)
        # 去重
        if any(_text_match_score(norm, e) > 0.6 for e in existing_statements):
            continue
        evidence_ids = [m.id for m in mems[:5]]
        avg_importance = sum(m.importance for m in mems) / len(mems)
        seed = DispositionSeed(
            id=new_memory_id("disp", now),
            statement=statement,
            strength=round(min(0.6, avg_importance), 4),
            evidence_ids=evidence_ids,
            created_at=now_iso,
            updated_at=now_iso,
        )
        store.save_disposition(seed)
        results.append(seed)
        if len(results) >= limit:
            break

    return results


def _abstract_via_llm(
    store: MemoryStore,
    extractor: Callable[[str], Any],
    existing: list[DispositionSeed],
    existing_statements: set[str],
    limit: int,
    now: datetime,
    now_iso: str,
) -> list[DispositionSeed]:
    """LLM 驱动的种子抽象。"""
    # 收集高价值记忆作为输入
    semantic = sorted(
        [m for m in store.list_semantic(include_archived=False) if m.importance >= 0.6],
        key=lambda m: m.importance,
        reverse=True,
    )[:30]
    episodic = sorted(
        [m for m in store.list_episodic(include_archived=False) if m.importance >= 0.6],
        key=lambda m: m.importance,
        reverse=True,
    )[:15]

    prompt = _build_abstract_prompt(semantic, episodic, existing)
    try:
        response = extractor(prompt)
    except Exception:
        return []

    results: list[DispositionSeed] = []
    candidates = response if isinstance(response, list) else []
    if isinstance(response, dict):
        candidates = response.get("dispositions", response.get("seeds", []))

    for cand in candidates[:limit]:
        if not isinstance(cand, dict):
            continue
        statement = cand.get("statement", "")
        if not statement:
            continue
        norm = _normalize(statement)
        # 去重
        if any(_text_match_score(norm, e) > 0.6 for e in existing_statements):
            continue
        # 必须有 evidence
        evidence_ids = cand.get("evidence_ids", [])
        if not evidence_ids:
            continue
        seed = DispositionSeed(
            id=new_memory_id("disp", now),
            statement=statement[:200],
            strength=cand.get("strength", 0.5),
            evidence_ids=evidence_ids[:10],
            created_at=now_iso,
            updated_at=now_iso,
        )
        store.save_disposition(seed)
        results.append(seed)

    return results


def _build_abstract_prompt(
    semantic: list[SemanticMemory],
    episodic: list[EpisodicMemory],
    existing: list[DispositionSeed],
) -> str:
    existing_text = "\n".join(
        f"- {s.statement} (strength={s.strength:.2f})" for s in existing
    ) or "（暂无已有倾向）"
    semantic_text = "\n".join(
        f"- [{m.id}] {m.subject} {m.predicate} {m.object}（importance={m.importance:.2f}）"
        for m in semantic[:20]
    )
    episodic_text = "\n".join(
        f"- [{m.id}] {m.summary}（importance={m.importance:.2f}, emotions={m.emotions}）"
        for m in episodic[:10]
    )
    return f"""你是一个心理倾向分析师。从以下长期记忆中，发现反复出现的、可泛化的行为倾向模式。

已有倾向：
{existing_text}

语义记忆（高重要性）：
{semantic_text}

情节记忆（高重要性）：
{episodic_text}

请输出 1-3 条新发现的倾向种子。每条必须：
- statement：一句话描述倾向模式（≤80字），可泛化、非单次事件
- strength：初始强度 0.4-0.7
- evidence_ids：支撑该倾向的记忆 ID 列表

输出纯 JSON 数组：
[{{"statement": "...", "strength": 0.5, "evidence_ids": ["id1", "id2"]}}]"""