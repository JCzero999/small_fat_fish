"""v0.12.0 方案 B — FSRS 风格牢固度（solidity）。

与七因子保留度正交：七因子管「留不留、留在哪档」；牢固度管「当前多容易被想起」。
- 想起（进入注入/工具命中）→ stability 增长、retrievability=1.0、解冻
- 时间流逝 → retrievability = exp(-elapsed_days / stability)，低于阈值且非保护 → 冻结
- 冻结的记忆不进自动注入与忽然想起，但 search / list / 精确查询仍能查到
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .models import (
    EpisodicMemory,
    Memory,
    SelfState,
    SemanticMemory,
    is_unresolved_episode,
    parse_iso8601,
    to_iso8601,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class SolidityConfig:
    """牢固度参数（默认值即可用）。"""

    freeze_threshold: float = 0.35      # retrievability 低于此且非保护 → frozen
    max_stability: float = 365.0        # 天量级硬顶
    initial_stability: float = 1.0
    review_window_hours: float = 24.0   # 窗口内多次想起只算一次加固
    stability_gain: float = 0.35        # 每次有效复习的增益基数（对数防爆炸）
    min_stability: float = 0.5
    decay_throttle_hours: float = 6.0   # batch_decay 每 peer 最短间隔
    enabled: bool = True                # 总开关（False 时行为与 v0.11 完全一致）


def compute_retrievability(
    stability: float,
    last_review: str | None,
    now: datetime,
    config: SolidityConfig | None = None,
) -> float:
    """retrievability = exp(-elapsed_days / max(stability, min_stability))。

    无 last_review → 1.0（新记忆默认可提取，等第一次批量衰减才落账）。
    """
    cfg = config or SolidityConfig()
    if last_review is None:
        return 1.0
    try:
        moment = parse_iso8601(last_review)
    except ValueError:
        return 1.0
    elapsed_days = max(0.0, (now - moment).total_seconds() / 86400.0)
    divisor = max(float(stability), cfg.min_stability)
    value = math.exp(-elapsed_days / divisor)
    return min(1.0, max(0.0, value))


def apply_review(
    mem: SemanticMemory | EpisodicMemory,
    now: datetime,
    config: SolidityConfig | None = None,
) -> SemanticMemory | EpisodicMemory:
    """「想起」加固：窗口内去重；窗口外 review_count+1、stability 对数增长、解冻。

    不负责落库（返回修改后的对象，调用方写回）。
    """
    cfg = config or SolidityConfig()
    window = cfg.review_window_hours * 3600.0
    if mem.last_review is not None:
        try:
            elapsed = (now - parse_iso8601(mem.last_review)).total_seconds()
        except ValueError:
            elapsed = None
        if elapsed is not None and 0 <= elapsed < window:
            # 窗口内：只刷新可提取度，不重复加固
            mem.retrievability = 1.0
            mem.frozen = False
            return mem
    new_stability = min(
        cfg.max_stability,
        max(cfg.min_stability,
            mem.stability + cfg.stability_gain * (1.0 + math.log1p(mem.review_count))),
    )
    mem.stability = new_stability
    mem.retrievability = 1.0
    mem.review_count += 1
    mem.last_review = to_iso8601(now)
    mem.frozen = False
    return mem


def is_freeze_protected(
    mem: SemanticMemory | EpisodicMemory,
    *,
    self_state: SelfState | None = None,
    core_protected_ids: set[str] | None = None,
    goal_source_ids: set[str] | None = None,
) -> bool:
    """保护项永不自动冻结：protected / Core 引用 / 未平复情感事件 / 有效约定关联。"""
    if getattr(mem, "protected", False):
        return True
    if core_protected_ids and mem.id in core_protected_ids:
        return True
    if goal_source_ids and mem.id in goal_source_ids:
        return True
    if isinstance(mem, EpisodicMemory) and is_unresolved_episode(mem):
        return True
    return False


def apply_decay_and_freeze(
    mem: SemanticMemory | EpisodicMemory,
    now: datetime,
    config: SolidityConfig | None = None,
    *,
    protected: bool = False,
) -> SemanticMemory | EpisodicMemory:
    """重算 retrievability（FSRS 时钟：从第一次「想起」起走）；

    低于阈值且非保护 → frozen=True。保护项数值照常更新，但强制保持未冻结。
    从未被想起过（last_review=None）的记忆不衰减——等第一次复习才开始计时。
    """
    cfg = config or SolidityConfig()
    # 时钟优先级：上次想起 → 上次激活 → 创建时间（「久不提就淡」对从未复习的记忆同样生效）
    base = mem.last_review or mem.last_activated or (
        mem.created_at if isinstance(mem, SemanticMemory) else mem.timestamp
    )
    mem.retrievability = compute_retrievability(mem.stability, base, now, cfg)
    if protected:
        mem.frozen = False
    elif mem.retrievability < cfg.freeze_threshold:
        mem.frozen = True
    return mem
