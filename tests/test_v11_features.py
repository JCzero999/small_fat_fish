"""v0.11.0 生活感补全（方案 A）测试：A1 约定提醒 / A2 忽然想起 / A3 影子模式。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from extra_brain import ExtraBrain
from extra_brain.goals import (
    extract_goals_from_report,
    get_due_goals,
    mark_goal_done,
    parse_deadline_from_text,
    update_goal_statuses,
)
from extra_brain.models import (
    ActiveGoal,
    MemoryValidationError,
    ProactiveConfig,
    SelfState,
    new_memory_id,
    normalize_goal,
    parse_iso8601,
    to_iso8601,
)

TZ = timezone(timedelta(hours=8))  # 宿主本地时区（Asia/Shanghai）
NOW = datetime(2026, 11, 4, 10, 0, 0, tzinfo=TZ)  # 周三上午


def make_semantic(memory_id: str, **overrides):
    from extra_brain.models import SemanticMemory

    base = dict(
        id=memory_id, type="fact", subject="用户", predicate="喜欢", object="白茶",
        detail="测试事实。", importance=0.7, emotion_valence="neutral",
        emotion_intensity=0.0, confidence=0.9, source_episodic_ids=[],
        created_at=to_iso8601(NOW), last_activated=to_iso8601(NOW),
    )
    base.update(overrides)
    return SemanticMemory(**base)


def make_episodic(memory_id: str, **overrides):
    from extra_brain.models import EpisodicMemory

    base = dict(
        id=memory_id, timestamp=to_iso8601(NOW), summary="测试事件",
        key_moments=["关键时刻"], emotions=["平静"], importance=0.7,
        related_semantic_ids=[], participants=["user", "agent"],
    )
    base.update(overrides)
    return EpisodicMemory(**base)


# ── A1：deadline 解析 ──────────────────────────────────────────────


class ParseDeadlineTests(unittest.TestCase):
    def test_ming_tian(self):
        got = parse_deadline_from_text("明天提醒我面试", NOW)
        self.assertIsNotNone(got)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 11, 5))
        self.assertEqual(dt.hour, 20)

    def test_jin_wan(self):
        got = parse_deadline_from_text("今晚记得喝水", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual((dt.month, dt.day, dt.hour), (11, 4, 21))

    def test_ming_zao(self):
        got = parse_deadline_from_text("明早叫我起床", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual((dt.day, dt.hour), (5, 9))

    def test_hou_tian(self):
        got = parse_deadline_from_text("后天交报告", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual(dt.day, 6)

    def test_da_hou_tian(self):
        got = parse_deadline_from_text("大后天下雨", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual(dt.day, 7)

    def test_next_week_day(self):
        # NOW 是周三，下周五 → 11 月 13 日
        got = parse_deadline_from_text("下周五聚餐", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual((dt.month, dt.day, dt.weekday()), (11, 13, 4))

    def test_weekday_without_xia(self):
        # 「周五」在本周语境：NOW 周三 → 最近的周五 11-06
        got = parse_deadline_from_text("周五交材料", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual((dt.day, dt.weekday()), (6, 4))

    def test_month_day(self):
        got = parse_deadline_from_text("11月20日生日", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual((dt.month, dt.day), (11, 20))

    def test_days_later(self):
        got = parse_deadline_from_text("3天后复查", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual(dt.day, 7)

    def test_hours_later(self):
        got = parse_deadline_from_text("2小时后开会", NOW)
        dt = parse_iso8601(got).astimezone(TZ)
        self.assertEqual((dt.hour, dt.minute), (12, 0))

    def test_no_hint_returns_none(self):
        self.assertIsNone(parse_deadline_from_text("我喜欢吃火锅", NOW))

    def test_empty_and_naive_safe(self):
        self.assertIsNone(parse_deadline_from_text("", NOW))
        self.assertIsNone(
            parse_deadline_from_text("明天", datetime(2026, 11, 4, 10, 0))  # naive
        )

    def test_invalid_month_day_ignored(self):
        self.assertIsNone(parse_deadline_from_text("13月40日", NOW))


# ── A1：ActiveGoal 模型与兼容 ──────────────────────────────────────


class ActiveGoalModelTests(unittest.TestCase):
    def test_from_plain_string(self):
        g = normalize_goal("想知道面试结果")
        self.assertIsInstance(g, ActiveGoal)
        self.assertEqual(g.text, "想知道面试结果")
        self.assertIsNone(g.deadline)
        self.assertEqual(g.status, "open")

    def test_status_validation(self):
        with self.assertRaises(MemoryValidationError):
            ActiveGoal(text="x", status="weird")

    def test_deadline_normalized_to_utc_iso(self):
        g = ActiveGoal(text="交报告", deadline="2026-11-05T20:00:00+08:00")
        self.assertTrue(g.deadline.endswith("Z"))
        self.assertEqual(g.deadline, "2026-11-05T12:00:00.000Z")

    def test_invalid_deadline_raises(self):
        with self.assertRaises(MemoryValidationError):
            ActiveGoal(text="x", deadline="not-a-date")

    def test_string_equality_compat(self):
        g = ActiveGoal(text="等回消息")
        self.assertTrue(g == "等回消息")
        self.assertEqual(str(g), "等回消息")

    def test_selfstate_accepts_plain_strings(self):
        ss = SelfState(active_goals=["a", "b"])
        self.assertTrue(all(isinstance(g, ActiveGoal) for g in ss.active_goals))
        # 旧格式兼容：无 deadline 的 open 目标序列化为纯字符串
        self.assertEqual(ss.to_dict()["active_goals"], ["a", "b"])

    def test_selfstate_roundtrip_with_deadline(self):
        ss = SelfState(active_goals=[{
            "text": "明天交报告", "deadline": "2026-11-05T12:00:00Z",
            "status": "open", "id": "goal_x", "created_at": "2026-11-04T10:00:00Z",
        }])
        d = ss.to_dict()
        self.assertIsInstance(d["active_goals"][0], dict)
        restored = SelfState.from_dict(d)
        self.assertEqual(restored.active_goals[0].deadline, "2026-11-05T12:00:00.000Z")
        self.assertEqual(restored.active_goals[0].id, "goal_x")

    def test_reminded_not_compacted(self):
        ss = SelfState(active_goals=[ActiveGoal(text="已提醒的事", status="reminded")])
        item = ss.to_dict()["active_goals"][0]
        self.assertIsInstance(item, dict)
        self.assertEqual(item["status"], "reminded")


class GoalStoreCompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.brain = ExtraBrain(Path(self.tmp.name) / "goal.db")

    def tearDown(self):
        self.brain.close()
        self.tmp.cleanup()

    def test_old_string_goals_survive_reload(self):
        # 模拟旧库：JSON 里是 list[str]
        raw = {"energy": 0.8, "mood_baseline": "平静", "active_goals": ["旧目标一", "旧目标二"],
               "self_narrative": "", "updated_at": "2026-11-01T00:00:00Z", "version": 1}
        self.brain.save_self_state(SelfState.from_dict(raw))
        ss = self.brain.get_self_state()
        self.assertEqual([g.text for g in ss.active_goals], ["旧目标一", "旧目标二"])

    def test_goals_capped_at_5(self):
        ss = SelfState(active_goals=[f"g{i}" for i in range(8)])
        self.assertEqual(len(ss.active_goals), 5)


# ── A1：合并 / 状态机 / 到期 ───────────────────────────────────────


class GoalMergeTests(unittest.TestCase):
    def test_new_goal_gets_deadline(self):
        goals = extract_goals_from_report(["明天交报告"], [], NOW)
        self.assertEqual(len(goals), 1)
        self.assertIsNotNone(goals[0].deadline)

    def test_dedup_similar_text(self):
        existing = [ActiveGoal(text="等小舟面试结果")]
        goals = extract_goals_from_report(["等小舟面试结果怎么样"], existing, NOW)
        self.assertEqual(len(goals), 1)

    def test_backfill_deadline_on_existing(self):
        existing = [ActiveGoal(text="明天交报告")]
        goals = extract_goals_from_report(["明天交报告"], existing, NOW)
        self.assertEqual(len(goals), 1)
        self.assertIsNotNone(goals[0].deadline)

    def test_cap_prefers_deadline(self):
        existing = [ActiveGoal(text=f"旧目标{i}", created_at="2026-11-01T00:00:00Z")
                    for i in range(5)]
        goals = extract_goals_from_report(["明天有个重要会议"], existing, NOW)
        self.assertEqual(len(goals), 5)
        self.assertTrue(any(g.deadline for g in goals))

    def test_mark_done(self):
        goals = [ActiveGoal(text="交周报"), ActiveGoal(text="买菜")]
        out = mark_goal_done(goals, "周报")
        self.assertEqual(out[0].status, "done")
        self.assertEqual(out[1].status, "open")

    def test_reminded_ages_to_expired(self):
        dl = to_iso8601(NOW - timedelta(days=4))
        goals = [ActiveGoal(text="过期约定", deadline=dl, status="reminded",
                            created_at=to_iso8601(NOW))]
        out = update_goal_statuses(goals, NOW)
        self.assertEqual(out[0].status, "expired")

    def test_get_due_goals_sorted(self):
        near = ActiveGoal(text="近", deadline=to_iso8601(NOW - timedelta(hours=1)),
                          created_at=to_iso8601(NOW))
        far = ActiveGoal(text="远", deadline=to_iso8601(NOW - timedelta(hours=10)),
                         created_at=to_iso8601(NOW))
        future = ActiveGoal(text="未到", deadline=to_iso8601(NOW + timedelta(days=1)),
                            created_at=to_iso8601(NOW))
        ss = SelfState(active_goals=[near, far, future], updated_at="2026-11-04T00:00:00Z")
        due = get_due_goals(ss, NOW)
        self.assertEqual([g.text for g in due], ["远", "近"])


# ── A1/A2：主动开口决策链 ──────────────────────────────────────────


class ProactivePriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.brain = ExtraBrain(Path(self.tmp.name) / "pro.db")
        cfg = self.brain.ensure_proactive_config("peer_p")
        cfg.enabled = True
        cfg.silence_threshold_minutes = 0
        cfg.min_interval_minutes = 0
        cfg.quiet_hours_start = 0
        cfg.quiet_hours_end = 0
        cfg.daily_limit = 10
        self.brain.save_proactive_config(cfg)

    def tearDown(self):
        self.brain.close()
        self.tmp.cleanup()

    def _open_window(self):
        cfg = self.brain.get_proactive_config_by_user("peer_p")
        cfg.sudden_recall_probability = 0.0
        cfg.sudden_recall_enabled = False
        self.brain.save_proactive_config(cfg)

    def test_due_goal_highest_priority(self):
        self._open_window()
        dl = to_iso8601(NOW - timedelta(minutes=5))
        self.brain.save_self_state(SelfState(
            energy=0.85, updated_at="2026-11-04T00:00:00Z",
            active_goals=[ActiveGoal(text="明天交报告", deadline=dl,
                                     created_at=to_iso8601(NOW))],
        ))
        result = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
        self.assertTrue(result["should"])
        self.assertEqual(result["reason"], "due_goal")
        self.assertIn("约定提醒", result["prompt"])
        self.assertIn("明天交报告", result["prompt"])

    def test_goal_beats_unresolved(self):
        from extra_brain.storage import WriteBatch

        self._open_window()
        ep = make_episodic("ep_unres", emotions=["难过"], importance=0.8,
                           emotion_state="open")
        self.brain.store.apply_batch(WriteBatch(episodic=[ep], operation="test.seed"))
        dl = to_iso8601(NOW - timedelta(minutes=5))
        self.brain.save_self_state(SelfState(
            energy=0.85, updated_at="2026-11-04T00:00:00Z",
            active_goals=[ActiveGoal(text="取快递", deadline=dl, created_at=to_iso8601(NOW))],
        ))
        result = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
        self.assertEqual(result["reason"], "due_goal")

    def test_reminded_then_quota_blocks(self):
        self._open_window()
        dl = to_iso8601(NOW - timedelta(minutes=5))
        goal = ActiveGoal(text="交材料", deadline=dl, created_at=to_iso8601(NOW))
        self.brain.save_self_state(SelfState(
            energy=0.85, updated_at="2026-11-04T00:00:00Z", active_goals=[goal]))
        first = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
        self.assertEqual(first["reason"], "due_goal")
        self.assertTrue(self.brain.confirm_goal_reminder("peer_p", first["goal_id"], now=NOW))
        # 已 reminded → 不再触发
        second = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
        self.assertNotEqual(second["reason"], "due_goal")

    def test_daily_limit_blocks_goal_reminder(self):
        self._open_window()
        cfg = self.brain.get_proactive_config_by_user("peer_p")
        cfg.goal_reminder_daily_limit = 1
        cfg.goal_reminder_count = 1
        cfg.goal_reminder_reset_date = NOW.date().isoformat()
        self.brain.save_proactive_config(cfg)
        dl = to_iso8601(NOW - timedelta(minutes=5))
        self.brain.save_self_state(SelfState(
            energy=0.85, updated_at="2026-11-04T00:00:00Z",
            active_goals=[ActiveGoal(text="交材料2", deadline=dl, created_at=to_iso8601(NOW))]))
        result = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
        self.assertNotEqual(result["reason"], "due_goal")

    def test_future_goal_not_due(self):
        self._open_window()
        dl = to_iso8601(NOW + timedelta(days=1))
        self.brain.save_self_state(SelfState(
            energy=0.85, updated_at="2026-11-04T00:00:00Z",
            active_goals=[ActiveGoal(text="后天的会", deadline=dl, created_at=to_iso8601(NOW))]))
        result = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
        self.assertNotEqual(result["reason"], "due_goal")

    def test_sudden_recall_probability_zero_never_fires(self):
        from extra_brain.storage import WriteBatch

        ep = make_episodic("ep_old", summary="很久以前的事", importance=0.6)
        self.brain.store.apply_batch(WriteBatch(episodic=[ep], operation="test.seed2"))
        cfg = self.brain.get_proactive_config_by_user("peer_p")
        cfg.sudden_recall_probability = 0.0
        self.brain.save_proactive_config(cfg)
        for _ in range(5):
            result = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
            self.assertNotEqual(result["reason"], "sudden_recall")

    def test_sudden_recall_always_fires_when_p1(self):
        from extra_brain.storage import WriteBatch

        ep = make_episodic("ep_old2", summary="海边那次", importance=0.66,
                           emotions=["开心"])
        self.brain.store.apply_batch(WriteBatch(episodic=[ep], operation="test.seed3"))
        cfg = self.brain.get_proactive_config_by_user("peer_p")
        cfg.sudden_recall_probability = 1.0
        self.brain.save_proactive_config(cfg)
        result = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
        self.assertEqual(result["reason"], "sudden_recall")
        self.assertIn("[忽然想起]", result["prompt"])
        self.brain.confirm_sudden_recall("peer_p", result["sudden_recall_id"], now=NOW)
        # 冷却期内不再选中同一条
        result2 = self.brain.build_proactive_prompt("peer_p", now=NOW, local_hour=12)
        self.assertNotEqual(result2.get("sudden_recall_id"), result["sudden_recall_id"])


# ── A2：pick_sudden_recall 独立行为 ────────────────────────────────


class SuddenRecallTests(unittest.TestCase):
    def setUp(self):
        from extra_brain.storage import WriteBatch

        self.tmp = tempfile.TemporaryDirectory()
        self.brain = ExtraBrain(Path(self.tmp.name) / "sr.db")
        cfg = self.brain.ensure_proactive_config("peer_sr")
        cfg.sudden_recall_probability = 1.0
        cfg.sudden_recall_daily_limit = 2
        cfg.sudden_recall_cooldown_days = 7
        self.brain.save_proactive_config(cfg)
        batch = WriteBatch(operation="test.seed_sr")
        for i in range(4):
            batch.episodic.append(make_episodic(f"ep_{i}", summary=f"事件{i}",
                                                importance=0.5 + i * 0.1))
        self.brain.store.apply_batch(batch)

    def tearDown(self):
        self.brain.close()
        self.tmp.cleanup()

    def test_no_candidate_returns_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        empty = ExtraBrain(Path(tmp.name) / "empty.db")
        self.addCleanup(empty.close)
        empty.ensure_proactive_config("peer_e")
        self.assertIsNone(empty.pick_sudden_recall("peer_e", NOW, local_hour=12))

    def test_quiet_hours_blocked(self):
        self.assertIsNone(self.brain.pick_sudden_recall("peer_sr", NOW, local_hour=23))

    def test_daily_limit(self):
        first = self.brain.pick_sudden_recall("peer_sr", NOW, local_hour=12)
        self.assertIsNotNone(first)
        self.brain.record_sudden_recall("peer_sr", first.id, now=NOW)
        second = self.brain.pick_sudden_recall("peer_sr", NOW, local_hour=12)
        self.assertIsNotNone(second)
        self.brain.record_sudden_recall("peer_sr", second.id, now=NOW)
        third = self.brain.pick_sudden_recall("peer_sr", NOW, local_hour=12)
        self.assertIsNone(third)

    def test_cooldown_excludes_recent(self):
        first = self.brain.pick_sudden_recall("peer_sr", NOW, local_hour=12)
        self.brain.record_sudden_recall("peer_sr", first.id, now=NOW)
        later = NOW + timedelta(days=1)
        cfg = self.brain.get_proactive_config_by_user("peer_sr")
        cfg.sudden_recall_count = 0  # 重置额度，只验冷却
        self.brain.save_proactive_config(cfg)
        picked_ids = set()
        for _ in range(10):
            mem = self.brain.pick_sudden_recall("peer_sr", later, local_hour=12)
            if mem:
                picked_ids.add(mem.id)
        self.assertNotIn(first.id, picked_ids)

    def test_cooldown_expiry_allows_again(self):
        first = self.brain.pick_sudden_recall("peer_sr", NOW, local_hour=12)
        self.brain.record_sudden_recall("peer_sr", first.id, now=NOW)
        far = NOW + timedelta(days=8)
        cfg = self.brain.get_proactive_config_by_user("peer_sr")
        cfg.sudden_recall_count = 0
        cfg.sudden_recall_reset_date = far.date().isoformat()
        self.brain.save_proactive_config(cfg)
        found = any(
            (m := self.brain.pick_sudden_recall("peer_sr", far, local_hour=12)) is not None
            and m.id == first.id
            for _ in range(20)
        )
        self.assertTrue(found)

    def test_disabled_flag(self):
        cfg = self.brain.get_proactive_config_by_user("peer_sr")
        cfg.sudden_recall_enabled = False
        self.brain.save_proactive_config(cfg)
        self.assertIsNone(self.brain.pick_sudden_recall("peer_sr", NOW, local_hour=12))

    def test_exclude_ids_respected(self):
        mem = self.brain.pick_sudden_recall("peer_sr", NOW, local_hour=12,
                                            exclude_ids={"ep_0", "ep_1", "ep_2", "ep_3"})
        self.assertIsNone(mem)


# ── A3：影子模式 ──────────────────────────────────────────────────


class ShadowModeTests(unittest.TestCase):
    def setUp(self):
        from extra_brain.storage import WriteBatch

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "shadow.db"
        self.extra_brians: list[ExtraBrain] = []
        from extra_brain.config import MemoryConfig

        self.brain = ExtraBrain(self.db, config=MemoryConfig(min_retrieval_score=0.0))
        self.extra_brians.append(self.brain)
        self.brain.save_self_state(SelfState(
            energy=0.72, mood_baseline="开心",
            active_goals=["关心面试"], self_narrative="我和小舟聊完了。",
            updated_at="2026-11-04T00:00:00Z",
        ))
        sem = make_semantic("sem_shadow", subject="小舟", predicate="即将",
                            object="面试", detail="小舟下周面试，很紧张。")
        self.brain.store.apply_batch(WriteBatch(semantic=[sem], operation="test.shadow"))
        self.brain.reindex()

    def tearDown(self):
        for brain in self.extra_brians:
            try:
                brain.close()
            except Exception:
                pass
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def test_default_off_preserves_v010(self):
        self.assertFalse(self.brain.shadow_mode)
        injection = self.brain.build_injection("小舟面试")
        self.assertIn("这一刻的自己", injection.context)

    def test_shadow_on_suppresses_self_state(self):
        self.assertTrue(self.brain.set_shadow_mode(True))
        injection = self.brain.build_injection("小舟面试")
        self.assertNotIn("这一刻的自己", injection.context)
        # 记忆本体照常注入
        self.assertIn("面试", injection.context)

    def test_persistence_across_instances(self):
        self.brain.set_shadow_mode(True)
        self.brain.close()
        again = ExtraBrain(self.db)
        self.extra_brians.append(again)
        self.assertTrue(again.shadow_mode)

    def test_write_still_happens_in_shadow(self):
        # 影子模式下 save/get SelfState 不受影响（照常写库）
        self.brain.set_shadow_mode(True)
        self.brain.save_self_state(SelfState(
            energy=0.5, updated_at="2026-11-04T06:00:00Z",
            active_goals=["影子下仍记录"]))
        ss = self.brain.get_self_state()
        self.assertEqual(ss.active_goals[0].text, "影子下仍记录")

    def test_off_restores_injection(self):
        self.brain.set_shadow_mode(True)
        self.brain.set_shadow_mode(False)
        injection = self.brain.build_injection("小舟面试")
        self.assertIn("这一刻的自己", injection.context)

    def test_read_only_rejects_switch(self):
        ro = ExtraBrain(self.db, read_only=True)
        self.extra_brians.append(ro)
        with self.assertRaises(PermissionError):
            ro.set_shadow_mode(True)


# ── ProactiveConfig v0.11.0 字段兼容 ───────────────────────────────


class ProactiveConfigCompatTests(unittest.TestCase):
    def test_old_dict_loads_with_defaults(self):
        old = {
            "id": "pa_old", "user_id": "u1", "enabled": True,
            "min_interval_minutes": 60, "daily_limit": 5,
            "silence_threshold_minutes": 120, "quiet_hours_start": 22,
            "quiet_hours_end": 8, "last_proactive_at": None,
            "daily_count": 3, "daily_reset_date": "2026-11-04",
            "last_umo": None, "created_at": None,
        }
        cfg = ProactiveConfig.from_dict(old)
        self.assertTrue(cfg.sudden_recall_enabled)
        self.assertEqual(cfg.sudden_recall_daily_limit, 2)
        self.assertEqual(cfg.goal_reminder_daily_limit, 1)
        self.assertEqual(cfg.sudden_recall_probability, 0.20)

    def test_roundtrip(self):
        cfg = ProactiveConfig(id="pa_rt", user_id="u2",
                              sudden_recall_log=[{"id": "ep_1", "ts": to_iso8601(NOW)}])
        restored = ProactiveConfig.from_dict(cfg.to_dict())
        self.assertEqual(restored.sudden_recall_log, cfg.sudden_recall_log)
        self.assertEqual(restored.sudden_recall_probability, 0.20)

    def test_probability_range(self):
        with self.assertRaises(MemoryValidationError):
            ProactiveConfig(id="pa_x", user_id="u3", sudden_recall_probability=1.5)


if __name__ == "__main__":
    unittest.main()
