"""v0.12.0 方案 B（FSRS 风格牢固度）测试：字段兼容 / 加固 / 衰减 / 冻结 / 接入。"""

from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from extra_brain import ExtraBrain
from extra_brain.models import (
    EpisodicMemory,
    SemanticMemory,
    SelfState,
    parse_iso8601,
    to_iso8601,
)
from extra_brain.solidity import (
    SolidityConfig,
    apply_decay_and_freeze,
    apply_review,
    compute_retrievability,
    is_freeze_protected,
)

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=TZ)


def sem(memory_id: str = "sem_1", **ov):
    base = dict(
        id=memory_id, type="fact", subject="用户", predicate="喜欢", object="白茶",
        detail="测试。", importance=0.7, emotion_valence="neutral",
        emotion_intensity=0.0, confidence=0.9, source_episodic_ids=[],
        created_at=to_iso8601(NOW), last_activated=to_iso8601(NOW),
    )
    base.update(ov)
    return SemanticMemory(**base)


def epi(memory_id: str = "ep_1", **ov):
    base = dict(
        id=memory_id, timestamp=to_iso8601(NOW), summary="测试事件",
        key_moments=["关键时刻"], emotions=["平静"], importance=0.7,
        related_semantic_ids=[], participants=["user", "agent"],
    )
    base.update(ov)
    return EpisodicMemory(**base)


# ── B1 字段与兼容 ──────────────────────────────────────────────────


class FieldCompatTests(unittest.TestCase):
    def test_defaults(self):
        m = sem()
        self.assertEqual(m.stability, 1.0)
        self.assertEqual(m.retrievability, 1.0)
        self.assertIsNone(m.last_review)
        self.assertEqual(m.review_count, 0)
        self.assertFalse(m.frozen)

    def test_old_dict_loads_defaults(self):
        old = {"id": "sem_x", "type": "fact", "subject": "s", "predicate": "p",
               "object": "o", "detail": "d", "importance": 0.7,
               "emotion_valence": "neutral", "emotion_intensity": 0.0,
               "confidence": 0.9, "source_episodic_ids": [],
               "created_at": to_iso8601(NOW), "last_activated": to_iso8601(NOW)}
        m = SemanticMemory.from_dict(old)
        self.assertEqual(m.stability, 1.0)
        self.assertFalse(m.frozen)

    def test_roundtrip(self):
        m = sem(stability=5.0, retrievability=0.4,
                last_review=to_iso8601(NOW), review_count=3, frozen=True)
        r = SemanticMemory.from_dict(m.to_dict())
        self.assertEqual(r.stability, 5.0)
        self.assertAlmostEqual(r.retrievability, 0.4, places=6)
        self.assertEqual(r.review_count, 3)
        self.assertTrue(r.frozen)

    def test_episodic_same(self):
        e = epi(stability=2.5, retrievability=0.2, frozen=True)
        r = EpisodicMemory.from_dict(e.to_dict())
        self.assertEqual(r.stability, 2.5)
        self.assertTrue(r.frozen)

    def test_stability_must_be_positive(self):
        from extra_brain.models import MemoryValidationError

        with self.assertRaises(MemoryValidationError):
            sem(stability=0.0)
        with self.assertRaises(MemoryValidationError):
            sem(stability=-1.0)

    def test_retrievability_range(self):
        from extra_brain.models import MemoryValidationError

        with self.assertRaises(MemoryValidationError):
            sem(retrievability=1.5)


# ── B2 compute_retrievability ─────────────────────────────────────


class ComputeTests(unittest.TestCase):
    def test_no_last_review_is_one(self):
        self.assertEqual(compute_retrievability(1.0, None, NOW), 1.0)

    def test_just_reviewed(self):
        lr = to_iso8601(NOW)
        self.assertAlmostEqual(compute_retrievability(1.0, lr, NOW), 1.0, places=6)

    def test_one_day_stability_one(self):
        lr = to_iso8601(NOW - timedelta(days=1))
        self.assertAlmostEqual(compute_retrievability(1.0, lr, NOW),
                               math.exp(-1), places=5)

    def test_high_stability_slower_decay(self):
        lr = to_iso8601(NOW - timedelta(days=10))
        low = compute_retrievability(1.0, lr, NOW)
        high = compute_retrievability(10.0, lr, NOW)
        self.assertGreater(high, low)
        self.assertAlmostEqual(high, math.exp(-1), places=5)

    def test_clamped_zero_future(self):
        lr = to_iso8601(NOW + timedelta(days=5))
        self.assertAlmostEqual(compute_retrievability(1.0, lr, NOW), 1.0, places=6)


# ── B2 apply_review（想起加固）─────────────────────────────────────


class ReviewTests(unittest.TestCase):
    def test_first_review(self):
        m = sem()
        apply_review(m, NOW)
        self.assertEqual(m.review_count, 1)
        self.assertEqual(m.retrievability, 1.0)
        self.assertEqual(m.last_review, to_iso8601(NOW))
        self.assertGreater(m.stability, 1.0)

    def test_window_dedup(self):
        cfg = SolidityConfig(review_window_hours=24.0)
        m = sem()
        apply_review(m, NOW, cfg)
        count = m.review_count
        later = NOW + timedelta(hours=5)
        apply_review(m, later, cfg)
        self.assertEqual(m.review_count, count)  # 窗口内不重复计数

    def test_outside_window_adds(self):
        cfg = SolidityConfig(review_window_hours=24.0)
        m = sem()
        apply_review(m, NOW, cfg)
        later = NOW + timedelta(hours=25)
        apply_review(m, later, cfg)
        self.assertEqual(m.review_count, 2)

    def test_unfreeze_on_review(self):
        m = sem(frozen=True, retrievability=0.1)
        apply_review(m, NOW)
        self.assertFalse(m.frozen)
        self.assertEqual(m.retrievability, 1.0)

    def test_stability_capped(self):
        cfg = SolidityConfig(max_stability=5.0)
        m = sem(stability=4.9)
        for i in range(10):
            apply_review(m, NOW + timedelta(days=i * 2), cfg)
        self.assertLessEqual(m.stability, 5.0)

    def test_log_growth_monotonic(self):
        cfg = SolidityConfig(max_stability=365.0)
        values = []
        m = sem()
        for i in range(6):
            apply_review(m, NOW + timedelta(days=i * 30), cfg)
            values.append(m.stability)
        self.assertEqual(values, sorted(values))
        # 对数防爆炸公式：单次增量始终为正且远小于硬顶
        self.assertLess(values[-1], 365.0)


# ── B2 apply_decay_and_freeze ─────────────────────────────────────


class DecayFreezeTests(unittest.TestCase):
    def test_decay_below_threshold_freezes(self):
        m = sem(stability=1.0, last_review=to_iso8601(NOW - timedelta(days=2)))
        cfg = SolidityConfig(freeze_threshold=0.35)
        apply_decay_and_freeze(m, NOW, cfg, protected=False)
        self.assertLess(m.retrievability, 0.35)
        self.assertTrue(m.frozen)

    def test_decay_above_threshold_not_frozen(self):
        m = sem(stability=30.0, last_review=to_iso8601(NOW - timedelta(days=2)))
        apply_decay_and_freeze(m, NOW, SolidityConfig(), protected=False)
        self.assertFalse(m.frozen)

    def test_protected_never_freezes(self):
        m = sem(stability=1.0, last_review=to_iso8601(NOW - timedelta(days=10)))
        apply_decay_and_freeze(m, NOW, SolidityConfig(), protected=True)
        self.assertLess(m.retrievability, 0.35)
        self.assertFalse(m.frozen)

    def test_no_review_falls_back_to_clock(self):
        # last_review=None：退回 last_activated（这里=NOW），不衰减
        m = sem()
        apply_decay_and_freeze(m, NOW, SolidityConfig(), protected=False)
        self.assertAlmostEqual(m.retrievability, 1.0, places=6)
        self.assertFalse(m.frozen)


class ProtectionTests(unittest.TestCase):
    def test_protected_flag(self):
        self.assertTrue(is_freeze_protected(sem(protected=True)))

    def test_core_reference(self):
        self.assertTrue(is_freeze_protected(sem("sem_a"), core_protected_ids={"sem_a"}))

    def test_unresolved_episode(self):
        e = epi(emotion_state="open", emotions=["难过"], importance=0.8)
        self.assertTrue(is_freeze_protected(e))

    def test_goal_source(self):
        self.assertTrue(is_freeze_protected(sem("sem_g"), goal_source_ids={"sem_g"}))

    def test_plain_not_protected(self):
        self.assertFalse(is_freeze_protected(sem()))


# ── B3 引擎接入：注入排除 / 加固 / 批量 / 节流 ──────────────────────


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        from extra_brain.storage import WriteBatch

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.brain = ExtraBrain(Path(self.tmp.name) / "b.db")
        self.batch_cls = WriteBatch

    def tearDown(self):
        self.brain.close()
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def _seed(self, mem):
        b = self.batch_cls(operation="test.seed")
        if isinstance(mem, SemanticMemory):
            b.semantic.append(mem)
        else:
            b.episodic.append(mem)
        self.brain.store.apply_batch(b)
        self.brain.reindex()

    def test_frozen_excluded_from_injection(self):
        m = sem("sem_frozen", frozen=True)
        self._seed(m)
        injection = self.brain.build_injection("用户喜欢什么")
        self.assertNotIn("sem_frozen", injection.context)

    def test_frozen_searchable_explicitly(self):
        m = sem("sem_frozen2", frozen=True)
        self._seed(m)
        result = self.brain.retrieve("用户喜欢什么", exclude_frozen=False)
        self.assertTrue(any(i.memory.id == "sem_frozen2" for i in result.semantic))

    def test_injection_marks_reviewed(self):
        m = sem("sem_live", subject="小舟", predicate="面试", object="通过",
                detail="小舟面试通过了", importance=0.9)
        self._seed(m)
        stored = self.brain.store.get_memory("sem_live")
        self.assertEqual(stored.review_count, 0)
        self.brain.build_injection("小舟面试", token_limit=1800)
        after = self.brain.store.get_memory("sem_live")
        self.assertGreaterEqual(after.review_count, 1)
        self.assertEqual(after.retrievability, 1.0)

    def test_review_persists_and_unfreezes(self):
        m = sem("sem_uf", frozen=True, retrievability=0.1)
        self._seed(m)
        self.brain.mark_memories_reviewed(["sem_uf"], now=NOW)
        after = self.brain.store.get_memory("sem_uf")
        self.assertFalse(after.frozen)
        self.assertEqual(after.retrievability, 1.0)

    def test_batch_decay_summary(self):
        self._seed(sem("sem_d1", stability=1.0,
                       last_review=to_iso8601(NOW - timedelta(days=5))))
        self._seed(epi("ep_d2", protected=True, stability=1.0,
                      last_review=to_iso8601(NOW - timedelta(days=5))))
        summary = self.brain.batch_decay(now=NOW)
        self.assertGreaterEqual(summary["updated"], 2)
        self.assertGreaterEqual(summary["newly_frozen"], 1)
        self.assertGreaterEqual(summary["skipped_protected"], 1)
        self.assertTrue(self.brain.store.get_memory("sem_d1").frozen)
        self.assertFalse(self.brain.store.get_memory("ep_d2").frozen)

    def test_maybe_decay_throttles(self):
        self._seed(sem("sem_t", last_review=to_iso8601(NOW - timedelta(days=5))))
        first = self.brain.maybe_run_decay(now=NOW)
        self.assertIsNotNone(first)
        second = self.brain.maybe_run_decay(now=NOW + timedelta(hours=2))
        self.assertIsNone(second)  # 6h 节流
        third = self.brain.maybe_run_decay(now=NOW + timedelta(hours=7))
        self.assertIsNotNone(third)

    def test_set_memory_frozen(self):
        self._seed(sem("sem_m"))
        self.brain.set_memory_frozen("sem_m", frozen=True, now=NOW)
        self.assertTrue(self.brain.store.get_memory("sem_m").frozen)
        self.brain.set_memory_frozen("sem_m", frozen=False, now=NOW)
        after = self.brain.store.get_memory("sem_m")
        self.assertFalse(after.frozen)
        self.assertEqual(after.retrievability, 1.0)

    def test_disabled_switch_no_effect(self):
        cfg = SolidityConfig(enabled=False)
        brain = ExtraBrain(Path(self.tmp.name) / "b2.db", solidity=cfg)
        self.addCleanup(brain.close)
        m = sem("sem_off", frozen=True)
        b = self.batch_cls(operation="test.seed_off")
        b.semantic.append(m)
        brain.store.apply_batch(b)
        brain.reindex()
        # 关闭时不过滤 frozen、不加固
        injection = brain.build_injection("用户喜欢什么")
        self.assertIn("sem_off", injection.context)
        stored = brain.store.get_memory("sem_off")
        self.assertTrue(stored.frozen)

    def test_solidity_in_stats(self):
        self._seed(sem("sem_s1"))
        stats = self.brain.storage_stats()
        self.assertIn("solidity", stats)
        self.assertIn("high", stats["solidity"])
        self.assertIn("frozen", stats["solidity"])

    def test_sudden_recall_skips_frozen(self):
        from extra_brain.storage import WriteBatch

        e = epi("ep_frz", summary="旧事", importance=0.9, frozen=True)
        b = WriteBatch(operation="test.seed_frz")
        b.episodic.append(e)
        self.brain.store.apply_batch(b)
        cfg = self.brain.ensure_proactive_config("peer_b")
        cfg.sudden_recall_probability = 1.0
        self.brain.save_proactive_config(cfg)
        self.assertIsNone(self.brain.pick_sudden_recall("peer_b", NOW, local_hour=12))


if __name__ == "__main__":
    unittest.main()
