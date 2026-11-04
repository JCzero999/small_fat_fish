"""v0.9.0 SelfState — 疲劳完整化 + 人生阶段强化。

每次 consolidate 成功后调用 ``update_self_state()`` 更新能量、心情、
活跃目标、疲劳原因、未平复心结与自我叙事。

v0.9.0 新增：
- 多因素能量消耗/恢复（高情绪轮数 + 主动开口次数）
- fatigue_reason 可解释
- 未平复事件检测注入 NarrativeLayer
- energy < 0.25 / < 0.15 临界行为标签
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .consolidation import ConsolidationReport
from .models import (
    ActiveGoal,
    EpisodicMemory,
    SelfState,
    is_unresolved_episode,
    to_iso8601,
    utc_now,
)
from .storage import MemoryStore

# ── v0.9.0 可调参数 ─────────────────────────────────────────────────
_RECOVERY_PER_HOUR = 0.012          # 安静时段每小时恢复
_DRAIN_PER_HIGH_EMOTION = 0.07      # 每条高情绪（intensity≥0.7）情节消耗
_DRAIN_PER_TURN_INTENSE = 0.005     # 高强度对话每轮额外消耗
_INTENSE_TURN_THRESHOLD = 10        # 轮数达到此值算高强度对话
_HIGH_EMOTION_INTENSITY = 0.7       # emotion_intensity 阈值
_DAILY_RECOVERY_CAP = 0.20          # 单日恢复上限
_TAKE_REST_RECOVERY = 0.15          # 用户表达关心/让休息的恢复值
_CRITICAL_LOW = 0.25                # 主动开口大幅降低
_VERY_LOW = 0.15                    # 最低限度回应
# ────────────────────────────────────────────────────────────────────


def update_self_state(
    store: MemoryStore,
    report: ConsolidationReport,
    *,
    conversation_text: str = "",
    proactive_count_since_last: int = 0,
    user_shows_care: bool = False,
    now: datetime | None = None,
    _extractor: Any = None,
) -> SelfState:
    """基于本轮巩固结果更新 SelfState 并写回持久化。

    v0.9.0 规则：
    1. energy：高情绪消耗 + 对话轮数消耗 + 安静恢复 + 关心恢复，钳制 [0.15, 1.0]
    2. fatigue_reason：解释最近一次大幅下降的原因
    3. mood_baseline：优先未平复事件情绪 → 最近情绪 → 回归「平静」
    4. active_goals：高 importance semantic 抽取，上限 5
    5. unresolved：未平复情节合并进 narrative_layer.unresolved_threads
    6. self_narrative：仅显著变化时更新
    """
    now = now or utc_now()
    prev = store.get_self_state()

    # ── 1. energy — 多因素 ──────────────────────────────────────
    energy, fatigue_reason = _compute_energy(
        prev, store, report,
        conversation_text=conversation_text,
        proactive_count_since_last=proactive_count_since_last,
        user_shows_care=user_shows_care,
        now=now,
    )

    # ── 2. mood_baseline ─────────────────────────────────────────
    mood = _derive_mood(store, prev.mood_baseline)

    # ── 3. active_goals ──────────────────────────────────────────
    goals = _extract_goals(store, list(prev.active_goals), now)

    # ── 4. unresolved ────────────────────────────────────────────
    _sync_unresolved(store)

    # ── 5. self_narrative ────────────────────────────────────────
    narrative = _maybe_update_narrative(
        prev.self_narrative, energy, mood, goals, now
    )

    # ── 6. last_significant_event ────────────────────────────────
    event_id = _find_significant(store)

    state = SelfState(
        energy=round(energy, 4),
        mood_baseline=mood,
        active_goals=goals,
        self_narrative=narrative,
        last_significant_event_id=event_id,
        fatigue_reason=fatigue_reason,
        updated_at=to_iso8601(now),
        version=prev.version + 1,
    )
    store.save_self_state(state)
    return state


# ── v0.9.0 energy engine ────────────────────────────────────────────


def _compute_energy(
    prev: SelfState,
    store: MemoryStore,
    report: ConsolidationReport,
    *,
    conversation_text: str,
    proactive_count_since_last: int,
    user_shows_care: bool,
    now: datetime,
) -> tuple[float, str | None]:
    """多因素能量计算，返回 (energy, fatigue_reason | None)。"""
    energy = float(prev.energy)
    reasons: list[str] = []

    # 1a. 高情绪情节消耗
    high_emotion_count = _count_high_emotion_episodes(store)
    drain_emotion = high_emotion_count * _DRAIN_PER_HIGH_EMOTION
    if drain_emotion > 0:
        energy -= drain_emotion
        reasons.append(f"本轮触发{high_emotion_count}条高情绪记忆（-{drain_emotion:.3f}）")

    # 1b. 对话轮数消耗
    estimated_turns = max(1, len(conversation_text) // 50)
    if estimated_turns >= _INTENSE_TURN_THRESHOLD:
        drain_turns = (estimated_turns - 5) * _DRAIN_PER_TURN_INTENSE
        energy -= drain_turns
        reasons.append(f"高强度对话（{estimated_turns}轮，-{drain_turns:.3f}）")

    # 1c. 主动开口消耗（时间流逝恢复优先）
    if proactive_count_since_last > 0:
        drain_proactive = proactive_count_since_last * 0.015
        energy -= drain_proactive
        reasons.append(f"主动开口{proactive_count_since_last}次（-{drain_proactive:.3f}）")

    # 1d. 安静时段恢复（按距上次更新的小时数）
    if prev.updated_at:
        try:
            last_time = datetime.fromisoformat(prev.updated_at.replace("Z", "+00:00"))
            hours_passed = (now - last_time).total_seconds() / 3600.0
            if hours_passed > 0:
                recovery = min(hours_passed * _RECOVERY_PER_HOUR, _DAILY_RECOVERY_CAP)
                energy += recovery
        except ValueError:
            pass

    # 1e. 用户关心/让休息
    if user_shows_care:
        energy += _TAKE_REST_RECOVERY
        reasons.append(f"用户表达关心（+{_TAKE_REST_RECOVERY}）")

    # 钳制
    energy = max(prev._ENERGY_MIN, min(prev._ENERGY_MAX, energy))

    # 疲劳原因：取最近一条大幅下降的原因
    fatigue_reason: str | None = None
    energy_drop = prev.energy - energy
    if energy_drop >= 0.05:  # 显著下降
        fatigue_reason = "；".join(reasons) if reasons else "高消耗对话"
    elif reasons:
        # 保留旧的 fatigue_reason 如果 energy 仍在低位
        if energy < 0.4 and prev.fatigue_reason:
            fatigue_reason = prev.fatigue_reason
        elif energy < 0.35:
            fatigue_reason = "；".join(reasons) if reasons else "持续消耗"
        else:
            fatigue_reason = None
    else:
        fatigue_reason = None

    return energy, fatigue_reason


def _count_high_emotion_episodes(store: MemoryStore) -> int:
    """统计最近一批情节中有多少高情绪强度（importance 代理）。"""
    episodes = store.list_episodic(include_archived=False)
    if not episodes:
        return 0
    by_time = sorted(episodes, key=lambda e: e.timestamp, reverse=True)
    count = 0
    for ep in by_time[:15]:
        if ep.importance >= _HIGH_EMOTION_INTENSITY or (ep.emotions and len(ep.emotions) > 0):
            count += 1
    return count


# ── helpers ────────────────────────────────────────────────────────


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _derive_mood(
    store: MemoryStore,
    previous_mood: str,
) -> str:
    """优先未平复事件情绪 → 最近情节情绪 → 缓慢回归「平静」。"""
    episodes = store.list_episodic(include_archived=False)
    by_time = sorted(episodes, key=lambda e: e.timestamp, reverse=True)

    # 1. 未平复事件优先
    for ep in by_time[:20]:
        if is_unresolved_episode(ep) and ep.emotions:
            return ep.emotions[0]

    # 2. 最近有情绪的情节
    for ep in by_time[:5]:
        if ep.emotions:
            return ep.emotions[0]

    # 3. 缓慢回归「平静」
    if previous_mood != "平静":
        return "平静"

    return "平静"


def _extract_goals(
    store: MemoryStore,
    existing: list[ActiveGoal],
    now: datetime,
) -> list[ActiveGoal]:
    """从高 importance 语义记忆中抽取/合并目标（v0.11.0：结构化 + deadline）。"""
    from .goals import extract_goals_from_report, update_goal_statuses

    semantic = store.list_semantic(include_archived=False)
    candidates: list[str] = []
    source_texts: dict[str, str] = {}
    for mem in semantic:
        if mem.importance >= 0.7:
            goal_text = f"{mem.subject}：{mem.predicate} {mem.object}".strip()
            if len(goal_text) > 40:
                goal_text = goal_text[:37] + "..."
            candidates.append(goal_text)
            source_texts[goal_text] = f"{mem.subject} {mem.predicate} {mem.object}"

    merged = extract_goals_from_report(candidates, existing, now, source_texts=source_texts)
    return update_goal_statuses(merged, now)


def _sync_unresolved(store: MemoryStore) -> None:
    """扫描未平复情节，写入对应的 NarrativeLayer.unresolved_threads。"""
    episodes = store.list_episodic(include_archived=False)
    unresolved_ids: list[str] = []
    for ep in episodes:
        if is_unresolved_episode(ep):
            unresolved_ids.append(ep.id)

    # 找最后一个 narrative layer 并更新
    narratives = store.list_narratives()
    if narratives:
        latest = max(narratives, key=lambda n: n.last_updated)
        if set(latest.unresolved_threads) != set(unresolved_ids):
            latest.unresolved_threads = unresolved_ids
            latest.last_updated = to_iso8601(utc_now())
            store.save_narrative(latest)


def _maybe_update_narrative(
    prev_narrative: str,
    energy: float,
    mood: str,
    goals: list[ActiveGoal],
    now: datetime,
) -> str:
    """仅在有显著变化时才更新 self_narrative。"""
    if not prev_narrative:
        return _build_simple_narrative(energy, mood, goals)

    narrative = prev_narrative
    goal_texts = [g.text for g in goals]
    goals_changed = not any(t in prev_narrative for t in goal_texts) and goal_texts

    if goals_changed:
        narrative = _build_simple_narrative(energy, mood, goals)

    return narrative


def _build_simple_narrative(energy: float, mood: str, goals: list[ActiveGoal]) -> str:
    """构建规则版自我叙事（≤80 字）。"""
    energy_label = (
        "精力充沛" if energy >= 0.75
        else "有点累了" if energy < 0.25
        else "略感疲惫" if energy < 0.40
        else "状态平稳"
    )
    mood_part = f"，心情{mood}" if mood != "平静" else ""
    goals_part = ""
    if goals:
        g = goals[0]
        goals_part = f"；惦记着「{g.text[:20]}」"
    narrative = f"我{energy_label}{mood_part}{goals_part}。"
    return narrative[:80]


def _find_significant(store: MemoryStore) -> str | None:
    """找到最近 importance 最高且带情绪的情节记忆。"""
    episodes = store.list_episodic(include_archived=False)
    by_time = sorted(episodes, key=lambda e: e.timestamp, reverse=True)
    best: EpisodicMemory | None = None
    for ep in by_time[:5]:
        if ep.emotions:
            if best is None or ep.importance > best.importance:
                best = ep
    return best.id if best else None


# ── v0.9.0 公开工具 ─────────────────────────────────────────────────


def energy_tier(state: SelfState) -> str:
    """返回当前能量水平标签。"""
    if state.energy >= 0.75:
        return "充沛"
    if state.energy >= 0.40:
        return "平稳"
    if state.energy >= _CRITICAL_LOW:
        return "偏低"
    if state.energy > _VERY_LOW:
        return "疲惫"
    return "极度疲惫"


def count_unresolved(store: MemoryStore) -> int:
    """返回当前未平复事件数量。"""
    return sum(
        1 for ep in store.list_episodic(include_archived=False)
        if is_unresolved_episode(ep)
    )


def take_snapshot(store: MemoryStore, state: SelfState | None = None,
                 reason: str = "") -> str:
    """保存当前 SelfState 快照到 life_snapshots 表，返回快照 ID。"""
    if state is None:
        state = store.get_self_state()
    from .models import new_memory_id
    snapshot_id = new_memory_id("snap", utc_now())
    store.save_life_snapshot(snapshot_id, state, reason=reason)
    return snapshot_id


def list_snapshots(store: MemoryStore) -> list[dict]:
    """列出所有人生快照。"""
    return store.list_life_snapshots()


def rollback_to_snapshot(store: MemoryStore, snapshot_id: str) -> SelfState | None:
    """从快照恢复 SelfState。"""
    state = store.get_life_snapshot(snapshot_id)
    if state is not None:
        store.save_self_state(state)
    return state