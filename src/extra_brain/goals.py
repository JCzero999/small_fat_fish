"""v0.11.0 A1 — ActiveGoal：deadline 解析、目标合并、到期提醒。

设计原则（清单 §A1）：
- 解析不到 deadline 就返回 None，绝不抛异常；
- 到期后 goal 保持 open，由提醒逻辑处理（本版不自动 expired）；
- 上限 5：带 deadline 的 open 目标优先保留，其余按 created_at 新→旧；
- 去重：文本相似（互相包含或 difflib≥0.75）或同 source_memory_id。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from .models import (
    ActiveGoal,
    MemoryValidationError,
    normalize_goal,
    parse_iso8601,
    to_iso8601,
    utc_now,
)

# 约定/提醒触发词（命中才尝试解析 deadline）
_GOAL_HINTS = (
    "提醒我", "到时候", "记得", "约定", "明天", "后天", "今晚", "明早",
    "下周", "下个月", "周一", "周二", "周三", "周四", "周五", "周六", "周日",
    "_POINT_",  # placeholder never matches
)
_DATE_HINTS = (
    "今天", "今晚", "明早", "明天", "后天", "大后天", "下周", "下个月",
    "周一", "周二", "周三", "周四", "周五", "周六", "周日", "号", "月",
)

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "十一": 11, "十二": 12, "十三": 13, "十四": 14,
           "十五": 15, "十六": 16, "十七": 17, "十八": 18,
           "十九": 19, "二十": 20, "二十一": 21, "二十二": 22,
           "二十三": 23, "二十四": 24, "二十五": 25, "二十六": 26,
           "二十七": 27, "二十八": 28, "二十九": 29, "三十": 30,
           "三十一": 31}

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def parse_deadline_from_text(text: str, now: datetime, *, timezone_aware_local_hour: int | None = None) -> str | None:
    """从中文自然语言尝试解析绝对 deadline（UTC ISO8601），失败返回 None。

    ``now`` 应为宿主本地时区的 aware datetime（相对日期按它展开）。
    支持：今晚/明早/明天/后天/大后天/下周一~日/X月Y日/ISO 日期。
    约定时刻默认按当天 20:00（晚上提醒自然）；「明早」= 9:00；「今晚」= 21:00。
    """
    if not text:
        return None
    try:
        if now.tzinfo is None:
            return None
    except AttributeError:
        return None

    local = now
    date: datetime | None = None

    # 显式 ISO8601
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        try:
            date = datetime.strptime(m.group(0), "%Y-%m-%d").replace(tzinfo=now.tzinfo)
        except ValueError:
            date = None

    # X月Y日（可选年份）
    if date is None:
        m = re.search(r"(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)
        if m:
            year = int(m.group(1)) if m.group(1) else local.year
            try:
                candidate = datetime(year, int(m.group(2)), int(m.group(3)),
                                     tzinfo=now.tzinfo)
                if year == local.year and candidate < local - timedelta(days=330):
                    candidate = candidate.replace(year=local.year + 1)
                date = candidate
            except ValueError:
                date = None

    # 中文数字 X月Y日（如「十一月三日」）
    if date is None:
        m = re.search(r"([一二两三四五六七八九十]{1,3}|十一|十二|十三)月"
                      r"(二十[一二三四五六七八九]?|[一二三四五六七八九十]|三十[一]?)\s*[日号]", text)
        if m:
            mo = _CN_NUM.get(m.group(1))
            dy = _CN_NUM.get(m.group(2))
            if mo and dy:
                try:
                    date = datetime(local.year, mo, dy, tzinfo=now.tzinfo)
                except ValueError:
                    date = None

    hour = 20  # 默认约定时刻
    if date is None:
        if "今晚" in text:
            date = local.replace(hour=21, minute=0, second=0, microsecond=0)
        elif "明早" in text or "明天早" in text or "明天上午" in text:
            date = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        elif "大后天" in text:
            date = (local + timedelta(days=3)).replace(hour=hour, minute=0, second=0, microsecond=0)
        elif "后天" in text:
            date = (local + timedelta(days=2)).replace(hour=hour, minute=0, second=0, microsecond=0)
        elif "明天" in text or "明日" in text:
            date = (local + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
        elif "今天" in text:
            date = local.replace(hour=hour, minute=0, second=0, microsecond=0)

    # 下周X / 周X（未匹配到日期类时）
    if date is None:
        m = re.search(r"(下+)?(?:星期|周|礼拜)([一二三四五六日天])", text)
        if m:
            target = _WEEKDAY[m.group(2)]
            cur = local.weekday()
            if m.group(1):
                # 「下周五」= 下周里的那个星期几；「下下周五」每多一个「下」+7 天
                days_ahead = 7 - cur + target + (len(m.group(1)) - 1) * 7
            else:
                days_ahead = (target - cur) % 7 or 7
            date = (local + timedelta(days=days_ahead)).replace(
                hour=hour, minute=0, second=0, microsecond=0)

    # 「N 个小时后 / N 天后 / N 分钟后」
    if date is None:
        m = re.search(r"(\d+)\s*(个)?小时(?:之)?后", text)
        if m:
            date = local + timedelta(hours=int(m.group(1)))
        if date is None:
            m = re.search(r"(\d+)\s*分钟(?:之)?后", text)
            if m:
                date = local + timedelta(minutes=int(m.group(1)))
        if date is None:
            m = re.search(r"(\d+)\s*(?:个)?天(?:之)?后", text)
            if m:
                date = (local + timedelta(days=int(m.group(1)))).replace(
                    hour=hour, minute=0, second=0, microsecond=0)

    if date is None:
        return None

    # 解析结果若已过去（± 秒级误差容忍），仍返回（由提醒逻辑决定 due）
    try:
        return to_iso8601(date.astimezone(utc_now().tzinfo))
    except (ValueError, OverflowError):
        return None


def looks_like_agreement(text: str) -> bool:
    """是否是约定/提醒类语句（触发 deadline 解析的粗筛）。"""
    if not text:
        return False
    lowered = text
    if any(k in lowered for k in ("提醒", "到时候", "别忘", "记得", "约定", "说好")):
        return True
    return any(h in lowered for h in _DATE_HINTS if h != "_POINT_")


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.75


def extract_goals_from_report(
    new_texts: list[str],
    existing: list[ActiveGoal],
    now: datetime | None = None,
    *,
    source_texts: dict[str, str] | None = None,
) -> list[ActiveGoal]:
    """从候选文本（抽取/高重要性记忆）合并新约定目标进 existing。

    - 新约定：status=open，尝试 parse_deadline；
    - 去重：文本相似或已存在 → 若旧的无 deadline 而新的能解析出 deadline，补上；
    - 截断到 5（带 deadline 的 open 优先，其次 created_at 新→旧）。
    """
    now = now or utc_now()
    goals = [normalize_goal(g) for g in existing]
    for raw in new_texts:
        text = str(raw).strip()[:80]
        if not text:
            continue
        deadline = parse_deadline_from_text(
            source_texts.get(text, text) if source_texts else text, now)
        merged = False
        for g in goals:
            if g.text == text or _similar(g.text, text):
                if g.deadline is None and deadline is not None and g.status == "open":
                    g.deadline = deadline
                merged = True
                break
        if not merged:
            goals.append(ActiveGoal(
                text=text,
                deadline=deadline,
                created_at=to_iso8601(now),
            ))
    return _trim_goals(goals)


def update_goal_statuses(goals: list[ActiveGoal], now: datetime) -> list[ActiveGoal]:
    """deadline 已过但从未提醒的保持 open（提醒逻辑负责转 reminded）。

    reminded 超过 3 天的视为过期 → expired（不再占据名额/由头）。
    """
    result = []
    for g in goals:
        item = normalize_goal(g)
        if item.status == "reminded" and item.deadline is not None:
            try:
                if (now - parse_iso8601(item.deadline)).days >= 3:
                    item.status = "expired"
            except MemoryValidationError:
                pass
        result.append(item)
    return result


def get_due_goals(self_state, now: datetime) -> list[ActiveGoal]:
    """status==open 且 deadline<=now，按 deadline 升序。"""
    due = [g for g in self_state.active_goals if g.is_due(now)]
    return sorted(due, key=lambda g: g.deadline or "")


def mark_goal_done(goals: list[ActiveGoal], keyword: str, now: datetime | None = None) -> list[ActiveGoal]:
    """用户表达「已完成/不用提醒了」时按关键词文本匹配置 done。"""
    result = []
    for g in goals:
        item = normalize_goal(g)
        if item.status == "open" and keyword and (
                keyword in item.text or item.text in keyword or _similar(item.text, keyword)):
            item.status = "done"
        result.append(item)
    return result


def _trim_goals(goals: list[ActiveGoal]) -> list[ActiveGoal]:
    """上限 5：open+deadline（临近优先）→ open 无 deadline（新→旧）→ reminded → done/expired（各留 1 条审计）。"""
    def group(g: ActiveGoal) -> int:
        if g.status == "open" and g.deadline:
            return 0
        if g.status == "open":
            return 1
        if g.status == "reminded":
            return 2
        return 3

    # 分两轮稳定排序：先 created 新→旧，再按组号 + deadline 升序
    ordered = sorted(goals, key=lambda g: g.created_at or "", reverse=True)
    ordered.sort(key=lambda g: (group(g), (g.deadline or "") if group(g) == 0 else ""))
    keep: list[ActiveGoal] = []
    closed_seen = {"done": 0, "expired": 0}
    for g in ordered:
        if g.status in ("done", "expired"):
            if closed_seen[g.status] >= 1:
                continue
            closed_seen[g.status] += 1
        keep.append(g)
    return keep[:5]
