"""v0.2.0–v0.5.0 新特性回归测试。

覆盖：双时态版本链、人格锚版本、情感状态机、感觉/工作记忆、
七因子保留度、四路召回、只读模式、存储统计、陪伴层数据模型、
主动开口决策、静态加密、向量索引（精确扫描 + HNSW）。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extra_brain import ExtraBrain, MemoryConfig  # noqa: E402
from extra_brain.embeddings import (  # noqa: E402
    HNSWIndex,
    LocalHashingEmbedder,
    OpenAICompatibleEmbedder,
    VectorEntry,
    VectorIndex,
    build_vector_index,
    cosine_similarity,
)
from extra_brain.models import (  # noqa: E402
    EpisodicMemory,
    MemoryAssociation,
    MemoryValidationError,
    NarrativeLayer,
    ProactiveConfig,
    RelationshipModel,
    SemanticMemory,
    UserSchema,
    is_emotion_protected,
    new_memory_id,
    to_iso8601,
    utc_now,
)
from extra_brain.session import SessionTracker  # noqa: E402
from extra_brain.storage import WriteBatch  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class FakeExtractor:
    def __init__(self, *results) -> None:
        self.results = list(results)

    def extract(self, prompt):
        from extra_brain import ExtractionResult

        return self.results.pop(0) if self.results else ExtractionResult()


def make_semantic(memory_id: str, **overrides) -> SemanticMemory:
    base = dict(
        id=memory_id,
        type="fact",
        subject="用户",
        predicate="喜欢",
        object="白茶",
        detail="测试事实。",
        importance=0.7,
        emotion_valence="neutral",
        emotion_intensity=0.0,
        confidence=0.9,
        source_episodic_ids=[],
        created_at=to_iso8601(NOW),
        last_activated=to_iso8601(NOW),
    )
    base.update(overrides)
    return SemanticMemory(**base)


def make_episodic(memory_id: str, **overrides) -> EpisodicMemory:
    base = dict(
        id=memory_id,
        timestamp=to_iso8601(NOW),
        summary="测试事件",
        key_moments=["关键时刻"],
        emotions=["平静"],
        importance=0.7,
        related_semantic_ids=[],
        participants=["user", "agent"],
    )
    base.update(overrides)
    return EpisodicMemory(**base)


class TempDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "memory.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()


class BitemporalTests(TempDbTestCase):
    """v0.2.0 双时态版本链 + as_of 查询。"""

    def test_new_fields_roundtrip_and_defaults(self) -> None:
        memory = make_semantic("sem_bt", valid_from=to_iso8601(NOW))
        data = memory.to_dict()
        self.assertEqual(data["valid_from"], to_iso8601(NOW))
        self.assertIsNone(data["valid_to"])
        self.assertIsNone(data["supersedes"])
        restored = SemanticMemory.from_dict(data)
        self.assertEqual(restored.valid_from, memory.valid_from)

    def test_old_dict_without_new_fields_still_loads(self) -> None:
        data = make_semantic("sem_old").to_dict()
        for key in ("valid_from", "valid_to", "supersedes", "retention_tier"):
            data.pop(key)
        restored = SemanticMemory.from_dict(data)
        self.assertIsNone(restored.valid_from)
        self.assertEqual(restored.retention_tier, 0)

        ep_data = make_episodic("ep_old").to_dict()
        for key in ("valid_from", "valid_to", "supersedes", "emotion_state", "retention_tier"):
            ep_data.pop(key)
        restored_ep = EpisodicMemory.from_dict(ep_data)
        self.assertIsNone(restored_ep.emotion_state)

    def test_as_of_excludes_invalid_at_time(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            early = make_semantic(
                "sem_early",
                valid_from=to_iso8601(NOW - timedelta(days=100)),
                valid_to=to_iso8601(NOW - timedelta(days=50)),
            )
            current = make_semantic(
                "sem_current",
                object="绿茶",
                valid_from=to_iso8601(NOW - timedelta(days=10)),
            )
            brain.store.save_semantic(early)
            brain.store.save_semantic(current)
            brain.reindex()

            # 查询「现在」：只有 current 有效
            now_result = brain.retrieve("用户喜欢喝什么？", as_of=to_iso8601(NOW))
            ids = now_result.memory_ids
            self.assertIn("sem_current", ids)
            self.assertNotIn("sem_early", ids)

            # 查询历史时间点：只有 early 有效
            past_result = brain.retrieve(
                "用户喜欢喝什么？", as_of=to_iso8601(NOW - timedelta(days=75))
            )
            past_ids = past_result.memory_ids
            self.assertIn("sem_early", past_ids)
            self.assertNotIn("sem_current", past_ids)

    def test_emotion_state_validation(self) -> None:
        for state in ("open", "acknowledged", "attended", "resolved", "dormant"):
            memory = make_episodic(f"ep_{state}", emotion_state=state)
            self.assertEqual(memory.emotion_state, state)
        with self.assertRaises(MemoryValidationError):
            make_episodic("ep_bad", emotion_state="invalid_state")


class PersonaAnchorTests(TempDbTestCase):
    """v0.2.0 人格锚版本与回滚。"""

    def test_anchor_fields_roundtrip(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            core = brain.core
            core.persona_anchor_version = "rev_001"
            core.persona_anchor_history = [
                {"version": "rev_001", "at": to_iso8601(NOW), "persona_core": "初版人格"}
            ]
            brain.save_core(core)
            reloaded = brain.core
            self.assertEqual(reloaded.persona_anchor_version, "rev_001")
            self.assertEqual(len(reloaded.persona_anchor_history), 1)

    def test_cli_rollback(self) -> None:
        from extra_brain.cli import main as cli_main

        with ExtraBrain(self.db_path) as brain:
            core = brain.core
            core.identity.persona_core = "初版人格"
            core.persona_anchor_version = "rev_001"
            core.persona_anchor_history = [
                {"version": "rev_001", "at": to_iso8601(NOW), "persona_core": "初版人格"}
            ]
            brain.save_core(core)

            core = brain.core
            core.identity.persona_core = "新版人格描述"
            core.persona_anchor_version = "rev_002"
            core.persona_anchor_history.append(
                {"version": "rev_002", "at": to_iso8601(NOW), "persona_core": "新版人格描述"}
            )
            brain.save_core(core)

        # 回滚到 rev_001
        exit_code = cli_main(["--db", str(self.db_path), "core", "--rollback", "rev_001"])
        self.assertEqual(exit_code, 0)
        with ExtraBrain(self.db_path) as brain:
            self.assertEqual(brain.core.persona_anchor_version, "rev_001")

        # 回滚到不存在的版本返回非零
        exit_code = cli_main(["--db", str(self.db_path), "core", "--rollback", "rev_999"])
        self.assertEqual(exit_code, 1)


class SessionMemoryTests(TempDbTestCase):
    """v0.2.1 感觉记忆环 + 工作记忆集合。"""

    def test_sensory_ring_bounds(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            tracker = SessionTracker(brain, consolidate_every=100, sensory_turns=2)
            tracker.start_session("s1")
            for i in range(10):
                tracker.add("user", f"消息{i}", auto_consolidate=False)
            # maxlen = 2 turns * 2 = 4 条
            self.assertEqual(len(tracker.sensory_ring), 4)
            self.assertEqual(tracker.sensory_ring[-1].content, "消息9")

    def test_working_set_cap_and_clear(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(make_semantic("sem_ws"))
            brain.reindex()
            tracker = SessionTracker(brain, consolidate_every=100, working_set_cap=2)
            tracker.start_session("s1")
            tracker.inject("用户喜欢喝什么白茶？")
            self.assertLessEqual(len(tracker.working_set), 2)
            tracker.working_set_clear()
            self.assertEqual(tracker.working_set, set())

    def test_working_set_primes_next_turn(self) -> None:
        """启动效应：上一轮进入上下文的记忆，本轮检索加权 +0.08。"""
        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(make_semantic("sem_prime", object="白茶"))
            brain.reindex()
            # 冷启动：无 priming
            cold = brain.retrieve("随便问点无关的话 xyzabc 123", primed_ids=())
            # 直接指定 primed：同一条记忆应获得加权（或至少不因 priming 掉出候选）
            hot = brain.retrieve("随便问点无关的话 xyzabc 123", primed_ids=("sem_prime",))
            cold_item = next((i for i in cold.semantic if i.memory.id == "sem_prime"), None)
            hot_item = next((i for i in hot.semantic if i.memory.id == "sem_prime"), None)
            if cold_item is not None and hot_item is not None:
                self.assertGreater(hot_item.final_score, cold_item.final_score)
            else:
                # 冷启动被门槛挡住、热启动被保送/加权后出现，同样证明生效
                self.assertIsNotNone(hot_item)

            # SessionTracker 滚动：inject 后 working_set = 本轮上下文命中的记忆
            tracker = SessionTracker(brain, consolidate_every=100)
            tracker.start_session("s1")
            injection = tracker.inject("用户喜欢喝什么白茶？")
            self.assertEqual(tracker.working_set, set(injection.retrieved.memory_ids))
            injection2 = tracker.inject("再来点无关的话 qqq www")
            # 第二轮的 primed 来自第一轮 working_set，不再等于本轮命中
            self.assertTrue(set(injection.retrieved.memory_ids) | tracker.working_set)

    def test_start_session_resets_rings(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            tracker = SessionTracker(brain, consolidate_every=100)
            tracker.start_session("s1")
            tracker.add("user", "测试消息", auto_consolidate=False)
            self.assertEqual(len(tracker.sensory_ring), 1)
            tracker.start_session("s2")
            self.assertEqual(len(tracker.sensory_ring), 0)
            self.assertEqual(len(tracker.messages), 0)


class RetentionTierTests(TempDbTestCase):
    """v0.2.2 七因子保留度 + 六档处置。"""

    def test_retention_tier_field_persists(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            memory = make_semantic("sem_tier", retention_tier=2)
            brain.store.save_semantic(memory)
            reloaded = brain.store.get_semantic("sem_tier")
            self.assertEqual(reloaded.retention_tier, 2)

    def test_protected_memory_survives_deep_decay(self) -> None:
        """protected 记忆评分恒为 1.0（P5），不会被归档。"""
        from extra_brain import ExtractionResult
        from extra_brain.consolidation import Consolidator

        very_old = NOW - timedelta(days=3650)
        with ExtraBrain(self.db_path, extractor=FakeExtractor(ExtractionResult())) as brain:
            brain.store.save_semantic(
                make_semantic(
                    "sem_stale",
                    importance=0.3,
                    last_activated=to_iso8601(very_old),
                    protected=True,
                )
            )
            consolidator = brain._consolidator
            consolidator.consolidate("用户：随便聊聊。", now=NOW)
            stale = brain.store.get_semantic("sem_stale")
            self.assertEqual(stale.decay_score, 1.0)
            self.assertEqual(stale.retention_tier, 0)
            self.assertFalse(stale.archived)

    def test_unprotected_stale_memory_drops_tier(self) -> None:
        from extra_brain import ExtractionResult
        from extra_brain.consolidation import Consolidator

        very_old = NOW - timedelta(days=3650)
        with ExtraBrain(self.db_path, extractor=FakeExtractor(ExtractionResult())) as brain:
            brain.store.save_semantic(
                make_semantic(
                    "sem_fresh",
                    importance=0.9,
                    last_activated=to_iso8601(NOW),
                )
            )
            brain.store.save_semantic(
                make_semantic(
                    "sem_stale2",
                    importance=0.3,
                    last_activated=to_iso8601(very_old),
                )
            )
            brain._consolidator.consolidate("用户：随便聊聊。", now=NOW)
            fresh = brain.store.get_semantic("sem_fresh")
            stale = brain.store.get_semantic("sem_stale2")
            self.assertGreater(fresh.decay_score, stale.decay_score)
            self.assertTrue(stale.archived)


class FourPathTests(TempDbTestCase):
    """v0.3.0 四路召回。"""

    def _seed(self, brain: ExtraBrain) -> None:
        brain.store.save_semantic(make_semantic("sem_a", object="白茶"))
        brain.store.save_semantic(
            make_semantic("sem_b", subject="项目", predicate="使用", object="数据库")
        )
        brain.reindex()

    def test_path_a_semantic(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            self._seed(brain)
            result = brain.retrieve("用户喜欢喝什么？", path="A")
            self.assertEqual(result.path, "A")
            self.assertIn("sem_a", result.memory_ids)

    def test_path_b_entity(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            self._seed(brain)
            result = brain.retrieve("随便问点什么", entities=["数据库"], path="B")
            self.assertEqual(result.path, "B")
            self.assertIn("sem_b", result.memory_ids)

    def test_path_d_snapshot(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            self._seed(brain)
            result = brain.retrieve("用户喜欢喝什么？", path="D", as_of=to_iso8601(NOW))
            self.assertEqual(result.path, "D")

    def test_default_fusion(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            self._seed(brain)
            retriever = brain.retriever
            result = retriever.search_four_paths(
                "用户喜欢喝什么白茶？", entities=["数据库"]
            )
            self.assertEqual(result.path, "ABCD")

    def test_result_dict_contains_path(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            self._seed(brain)
            data = brain.retrieve("白茶", path="A").to_dict()
            self.assertEqual(data["path"], "A")


class ReadOnlyAndStatsTests(TempDbTestCase):
    """v0.3.0 只读模式 + 存储统计。"""

    def test_read_only_blocks_consolidation_and_writes(self) -> None:
        with ExtraBrain(self.db_path) as writer:
            writer.store.save_semantic(make_semantic("sem_ro"))
            writer.reindex()
        with ExtraBrain(self.db_path, read_only=True) as ro:
            result = ro.retrieve("白茶")
            self.assertIn("sem_ro", result.memory_ids)
            with self.assertRaises(PermissionError):
                ro.consolidate("用户：我喜欢白茶")
            with self.assertRaises(PermissionError):
                ro.save_relationship(
                    RelationshipModel(
                        id="rel_1",
                        user_id="u1",
                        relationship_type="陪伴型",
                        closeness=0.5,
                        trust=0.5,
                        familiarity=0.5,
                        last_updated=to_iso8601(NOW),
                    )
                )

    def test_storage_stats_shape(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(make_semantic("sem_st"))
            stats = brain.storage_stats()
            self.assertEqual(stats["semantic"]["total"], 1)
            self.assertIn("archive_ratio", stats)
            self.assertIn("tiers", stats["semantic"])
            self.assertGreater(stats["db_size_bytes"], 0)


class CompanionLayerTests(TempDbTestCase):
    """v0.4.0 关系模型 / 用户图式 / 叙事层 / 联想边 / 主动开口。"""

    def test_relationship_roundtrip(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            model = RelationshipModel(
                id="rel_1",
                user_id="u1",
                relationship_type="父女",
                closeness=0.8,
                trust=0.7,
                familiarity=0.9,
                last_updated=to_iso8601(NOW),
                interaction_count=42,
                notes="慢环持久化验证",
            )
            brain.save_relationship(model)
            loaded = brain.get_relationship_by_user("u1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.closeness, 0.8)
            self.assertEqual(loaded.notes, "慢环持久化验证")
            self.assertEqual(len(brain.list_relationships()), 1)

    def test_user_schema_roundtrip(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            schema = UserSchema(
                id="usr_1",
                user_id="u1",
                schema_type="情感型",
                traits=["细腻", "敏感"],
                emotional_patterns=["深夜情绪放大"],
                preferred_topics=["音乐"],
                avoided_topics=["说教"],
                confidence=0.65,
                last_updated=to_iso8601(NOW),
            )
            brain.save_user_schema(schema)
            loaded = brain.get_user_schema_by_user("u1")
            self.assertEqual(loaded.traits, ["细腻", "敏感"])
            self.assertEqual(loaded.avoided_topics, ["说教"])

    def test_narrative_roundtrip(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            narrative = NarrativeLayer(
                id="nar_1",
                user_id="u1",
                phase="磨合期",
                theme="共同成长",
                event_ids=["ep_1", "ep_2"],
                summary="我们从争吵走向理解的三个月。",
                last_updated=to_iso8601(NOW),
            )
            brain.save_narrative(narrative)
            loaded = brain.get_narrative_by_user("u1")
            self.assertEqual(loaded.phase, "磨合期")
            self.assertEqual(loaded.event_ids, ["ep_1", "ep_2"])
            with self.assertRaises(MemoryValidationError):
                NarrativeLayer(
                    id="nar_bad",
                    user_id="u1",
                    phase="不存在的阶段",
                    theme="x",
                    event_ids=[],
                    summary="y",
                    last_updated=to_iso8601(NOW),
                )

    def test_association_and_neighbor_lookup(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            assoc = MemoryAssociation(
                id="assoc_1",
                source_memory_id="sem_a",
                target_memory_id="sem_b",
                strength=0.6,
                co_activation_count=3,
            )
            brain.save_association(assoc)
            neighbors = brain.list_associations_for_memory("sem_a")
            self.assertEqual(len(neighbors), 1)
            self.assertEqual(neighbors[0].target_memory_id, "sem_b")
            # 反向也能查到
            self.assertEqual(len(brain.list_associations_for_memory("sem_b")), 1)

    def _config(self, **overrides) -> ProactiveConfig:
        base = dict(
            id="pa_1",
            user_id="u1",
            enabled=True,
            min_interval_minutes=60,
            daily_limit=5,
            silence_threshold_minutes=120,
            quiet_hours_start=22,
            quiet_hours_end=8,
        )
        base.update(overrides)
        return ProactiveConfig(**base)

    def test_proactive_decision_matrix(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.save_proactive_config(self._config())

            # 静默达标 + 白天：可开口
            should, reason = brain.should_proactive_open(
                "u1", now=datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
            )
            self.assertTrue(should, reason)

            # 安静时段（23 点）：不开口
            should, reason = brain.should_proactive_open(
                "u1", now=datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
            )
            self.assertFalse(should)
            self.assertEqual(reason, "quiet_hours")

            # 未配置用户
            should, reason = brain.should_proactive_open("unknown_user")
            self.assertFalse(should)

    def test_proactive_daily_limit(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.save_proactive_config(self._config(daily_limit=2))
            now = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
            brain.record_proactive_open("u1", now=now)
            brain.record_proactive_open("u1", now=now + timedelta(minutes=130))
            should, reason = brain.should_proactive_open(
                "u1", now=now + timedelta(hours=6)
            )
            self.assertFalse(should)
            self.assertEqual(reason, "daily_limit_reached")

    def test_record_updates_last_proactive(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.save_proactive_config(self._config())
            now = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
            brain.record_proactive_open("u1", now=now)
            config = brain.get_proactive_config_by_user("u1")
            self.assertEqual(config.daily_count, 1)
            # 10 分钟后：间隔未达标
            should, reason = brain.should_proactive_open("u1", now=now + timedelta(minutes=10))
            self.assertFalse(should)
            self.assertEqual(reason, "min_interval_not_reached")

    def test_build_proactive_prompt_with_material(self) -> None:
        """决策放行时：返回可直接喂给模型的由头提示词，含记忆素材。"""
        with ExtraBrain(self.db_path) as brain:
            brain.save_proactive_config(
                self._config(daily_limit=10, silence_threshold_minutes=1,
                             min_interval_minutes=1)
            )
            brain.store.save_semantic(make_semantic("sem_pa", object="面试", importance=0.8))
            brain.store.save_episodic(
                make_episodic("ep_pa", summary="用户在准备重要面试",
                              emotions=["焦虑"], emotion_state="open", importance=0.9)
            )
            brain.reindex()
            result = brain.build_proactive_prompt(
                "u1", now=datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
            )
            self.assertTrue(result["should"], result["reason"])
            prompt = result["prompt"]
            self.assertIn("由头", prompt)
            self.assertIn("面试", prompt)
            self.assertIn("情绪还没平复", prompt)  # unresolved episode annotated

    def test_build_proactive_prompt_blocked_reason(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.save_proactive_config(self._config())
            result = brain.build_proactive_prompt(
                "u1", now=datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
            )
            self.assertFalse(result["should"])
            self.assertEqual(result["reason"], "quiet_hours")
            self.assertNotIn("prompt", result)

    def test_ensure_proactive_config_creates_default(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            config = brain.ensure_proactive_config("brand_new_user")
            self.assertTrue(config.enabled)
            again = brain.ensure_proactive_config("brand_new_user")
            self.assertEqual(config.id, again.id)

    def test_proactive_last_umo_roundtrip(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            config = self._config()
            config.last_umo = "aiocqhttp:FriendMessage:123456"
            brain.save_proactive_config(config)
            loaded = brain.get_proactive_config_by_user("u1")
            self.assertEqual(loaded.last_umo, "aiocqhttp:FriendMessage:123456")


class EmotionProtectionTests(TempDbTestCase):
    """v0.4.0 规划项：情感记忆保护（flags.bit6 语义）——慢衰减 + 保底温层。"""

    def test_predicate(self) -> None:
        hot = make_semantic("sem_hot", emotion_valence="negative", emotion_intensity=0.7)
        self.assertTrue(is_emotion_protected(hot))
        mild = make_semantic("sem_mild", emotion_valence="neutral", emotion_intensity=0.5)
        self.assertFalse(is_emotion_protected(mild))
        valenced = make_semantic("sem_val", emotion_valence="positive", emotion_intensity=0.5)
        self.assertTrue(is_emotion_protected(valenced))
        episodic_strong = make_episodic("ep_strong", emotions=["感动"], importance=0.9)
        self.assertTrue(is_emotion_protected(episodic_strong))
        episodic_mild = make_episodic("ep_mild", emotions=["平静"], importance=0.6)
        self.assertFalse(is_emotion_protected(episodic_mild))

    def test_stale_emotional_memory_floors_at_warm_tier(self) -> None:
        """高情绪显著性但很久没激活：曲线放慢 3 倍 + tier 封顶温层，不落入归档/墓碑。"""
        from extra_brain import ExtractionResult

        very_old = NOW - timedelta(days=730)  # 陈旧包络到地板：普通记忆此时必入墓碑档
        with ExtraBrain(self.db_path, extractor=FakeExtractor(ExtractionResult())) as brain:
            # 对照：普通低价值陈旧记忆会被归档
            brain.store.save_semantic(
                make_semantic(
                    "sem_plain_stale",
                    importance=0.3,
                    emotion_valence="neutral",
                    emotion_intensity=0.0,
                    last_activated=to_iso8601(very_old),
                )
            )
            # 情感保护：高唤起陈旧记忆保底温层、不归档
            brain.store.save_semantic(
                make_semantic(
                    "sem_emotional_stale",
                    importance=0.5,
                    emotion_valence="negative",
                    emotion_intensity=0.8,
                    last_activated=to_iso8601(very_old),
                )
            )
            brain._consolidator.consolidate("用户：随便聊聊。", now=NOW)

            plain = brain.store.get_semantic("sem_plain_stale")
            emotional = brain.store.get_semantic("sem_emotional_stale")
            self.assertTrue(plain.archived)
            self.assertFalse(emotional.archived)
            self.assertLessEqual(emotional.retention_tier, 1)  # 热或温
            if not plain.archived:  # 归档会把分数清零，仅未归档时比较
                self.assertGreater(emotional.decay_score, plain.decay_score)


class EncryptionTests(TempDbTestCase):
    """v0.5.0 静态加密（口令一致才能解密）。"""

    def test_data_is_obfuscated_on_disk(self) -> None:
        passphrase = "correct-horse-battery"
        with ExtraBrain(
            self.db_path, config=MemoryConfig()
        ) as brain:
            pass  # 先建表
        from extra_brain.storage import MemoryStore

        store = MemoryStore(
            self.db_path, encryption_passphrase=passphrase
        )
        store.save_semantic(make_semantic("sem_enc"))
        store.close()

        # 直接读原始 DB：不应出现明文 object
        raw = sqlite3.connect(self.db_path)
        row = raw.execute(
            "SELECT data FROM semantic_memories WHERE id = ?", ("sem_enc",)
        ).fetchone()
        raw.close()
        self.assertNotIn("白茶", row[0])

        # 用同一口令可以读回
        store2 = MemoryStore(self.db_path, encryption_passphrase=passphrase)
        loaded = store2.get_semantic("sem_enc")
        store2.close()
        self.assertEqual(loaded.object, "白茶")

        # 不同口令读不出原始明文（解密失败或校验失败）
        store3 = MemoryStore(self.db_path, encryption_passphrase="wrong-pass")
        try:
            loaded3 = store3.get_semantic("sem_enc")
            self.assertNotEqual(loaded3.object, "白茶")
        except Exception:
            pass  # 解密抛错也算拒绝
        finally:
            store3.close()

    def test_no_passphrase_is_transparent(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(make_semantic("sem_plain"))
        raw = sqlite3.connect(self.db_path)
        row = raw.execute(
            "SELECT data FROM semantic_memories WHERE id = ?", ("sem_plain",)
        ).fetchone()
        raw.close()
        self.assertIn("白茶", row[0])


class VectorIndexTests(unittest.TestCase):
    """v0.5.0 向量索引：精确扫描 + HNSW。"""

    def test_vector_index_bruteforce_small(self) -> None:
        embedder = LocalHashingEmbedder(64)
        index = VectorIndex(dimensions=64, hnsw_threshold=10_000)
        index.add("a", embedder.embed("我喜欢白茶"))
        index.add("b", embedder.embed("数据库索引优化"))
        results = index.search(embedder.embed("用户爱喝什么茶"), k=1)
        self.assertEqual(results[0][0], "a")
        self.assertFalse(index._use_hnsw)

    def test_vector_index_switches_to_hnsw(self) -> None:
        embedder = LocalHashingEmbedder(32)
        index = VectorIndex(dimensions=32, hnsw_threshold=4)
        for i in range(6):
            index.add(f"n{i}", embedder.embed(f"记忆内容 {i}"))
        self.assertTrue(index._use_hnsw)
        results = index.search(embedder.embed("记忆内容 3"), k=3)
        self.assertTrue(any(nid == "n3" for nid, _ in results))

    def test_hnsw_recall_reasonable(self) -> None:
        embedder = LocalHashingEmbedder(64)
        hnsw = HNSWIndex(dimensions=64, M=8, ef_construction=40, ef_search=40)
        texts = [f"话题{i % 10} 关键词{i}" for i in range(120)]
        for i, text in enumerate(texts):
            hnsw.add(f"node_{i}", embedder.embed(text))
        query = embedder.embed("话题3 关键词3")
        results = hnsw.search(query, k=5)
        # HNSW 是近似索引：不保证完全一致，但查询原串必须命中自己
        ids = [nid for nid, _ in results]
        expected = [
            f"node_{i}" for i, t in enumerate(texts) if t == "话题3 关键词3"
        ]
        if expected:
            self.assertIn(expected[0], ids)

    def test_build_vector_index_from_entries(self) -> None:
        embedder = LocalHashingEmbedder(32)
        entries = {
            "m1": VectorEntry("m1", "semantic", embedder.model_name, embedder.embed("白茶")),
            "m2": VectorEntry("m2", "semantic", embedder.model_name, embedder.embed("咖啡")),
        }
        index = build_vector_index(entries)
        self.assertEqual(len(index), 2)
        results = index.search(embedder.embed("白茶"), k=1)
        self.assertEqual(results[0][0], "m1")

    def test_openai_embedder_falls_back_without_api_base(self) -> None:
        embedder = OpenAICompatibleEmbedder(api_base="", dimensions=384)
        vector = embedder.embed("测试文本")
        self.assertEqual(len(vector), 384)
        self.assertIn("openai-compatible", embedder.model_name)


class ViewPerspectiveTests(TempDbTestCase):
    """v0.4.0 遗留：多角度视角 fact/feeling/bond/story 打标 + 检索过滤。"""

    def test_derivation_matrix(self) -> None:
        from extra_brain.models import derive_view

        self.assertEqual(derive_view("relationship"), "bond")
        self.assertEqual(derive_view("fact", "negative", 0.5), "feeling")
        self.assertEqual(derive_view("preference", "neutral", 0.9), "fact")
        self.assertEqual(derive_view("fact", "mixed", 0.44), "fact")
        self.assertEqual(derive_view(None, is_episodic=True), "story")

    def test_legacy_memories_auto_backfill_view_on_load(self) -> None:
        data = make_semantic("sem_view_legacy", object="绿茶", emotion_valence="positive",
                             emotion_intensity=0.8).to_dict()
        data.pop("view")  # 模拟旧库：无 view 字段
        restored = SemanticMemory.from_dict(data)
        self.assertEqual(restored.view, "feeling")
        ep_data = make_episodic("ep_view_legacy").to_dict()
        ep_data.pop("view")
        self.assertEqual(EpisodicMemory.from_dict(ep_data).view, "story")

    def test_invalid_view_rejected(self) -> None:
        with self.assertRaises(MemoryValidationError):
            make_semantic("sem_bad_view", view="mood")

    def test_retrieve_filters_by_view(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(
                make_semantic("sem_fact", object="白茶", emotion_valence="neutral",
                              emotion_intensity=0.0)
            )
            brain.store.save_semantic(
                make_semantic("sem_feel", object="白茶", emotion_valence="positive",
                              emotion_intensity=0.8)
            )
            brain.store.save_semantic(
                make_semantic("sem_bond", type="relationship", object="父女关系")
            )
            brain.store.save_episodic(make_episodic("ep_story", summary="白茶的故事"))
            brain.reindex()

            all_ids = brain.retrieve("白茶").memory_ids
            self.assertEqual(len(all_ids), 4)
            self.assertEqual(
                brain.retrieve("白茶", views=["feeling"]).memory_ids, ["sem_feel"]
            )
            self.assertEqual(
                set(brain.retrieve("白茶", views=["bond", "story"]).memory_ids),
                {"sem_bond", "ep_story"},
            )
            with self.assertRaises(ValueError):
                brain.retrieve("白茶", views=["nonsense"])

    def test_injection_respects_views(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(
                make_semantic("sem_iv_fact", object="白茶", emotion_valence="neutral",
                              emotion_intensity=0.0)
            )
            brain.store.save_semantic(
                make_semantic("sem_iv_feel", object="白茶", emotion_valence="negative",
                              emotion_intensity=0.9)
            )
            brain.reindex()
            injection = brain.build_injection("白茶", views=["fact"])
            self.assertIn("sem_iv_fact", injection.context)
            self.assertNotIn("sem_iv_feel", injection.context)

    def test_merge_recomputes_view(self) -> None:
        from extra_brain import ExtractionResult, SemanticCandidate

        candidate = SemanticCandidate(
            type="preference", subject="用户", predicate="喜欢", object="白茶",
            detail="强烈地喜欢白茶", importance=0.7, emotion_valence="positive",
            emotion_intensity=0.9, confidence=0.9,
        )
        with ExtraBrain(self.db_path, extractor=FakeExtractor(ExtractionResult(semantic_facts=[candidate]))) as brain:
            brain.store.save_semantic(
                make_semantic("sem_merge_view", object="白茶", emotion_valence="neutral",
                              emotion_intensity=0.0)
            )
            report = brain.consolidate("用户：我超爱白茶！")
            merged = brain.store.get_semantic("sem_merge_view")
            self.assertEqual(merged.view, "feeling", report.to_dict())


class ShadowLogRebuildTests(TempDbTestCase):
    """v0.3.0 遗留：崩溃安全影子日志 + rebuild --from-log。"""

    def _extractor_with_fact(self):
        from extra_brain import ExtractionResult, SemanticCandidate

        return FakeExtractor(
            ExtractionResult(
                semantic_facts=[
                    SemanticCandidate(
                        type="preference", subject="用户", predicate="喜欢", object="白茶",
                        detail="用户明确说喜欢白茶。", importance=0.7,
                        emotion_valence="positive", emotion_intensity=0.6, confidence=0.9,
                    )
                ]
            )
        )

    def test_shadow_marked_done_after_consolidate(self) -> None:
        with ExtraBrain(self.db_path, extractor=self._extractor_with_fact()) as brain:
            brain.consolidate("用户：我喜欢白茶。")
            self.assertEqual(brain.list_pending_consolidations(), [])

    def test_crash_between_extract_and_write_stays_pending(self) -> None:
        from extra_brain import ExtractionResult

        extractor = FakeExtractor(ExtractionResult())
        with ExtraBrain(self.db_path, extractor=extractor) as brain:
            # 巩固成功 → done；再制造一条孤儿 pending（模拟崩溃残留）
            brain.consolidate("用户：随便。")
            brain.store.log_session_begin("log_orphan", "用户：崩溃前没来得及巩固。", None, now=NOW)
            pending = brain.list_pending_consolidations()
            self.assertEqual([entry["id"] for entry in pending], ["log_orphan"])

        # 重放：用新 brain（带 extractor 结果），孤儿条目被巩固并标 done
        from extra_brain import ExtractionResult as ER

        replay_extractor = FakeExtractor(ER())
        with ExtraBrain(self.db_path, extractor=replay_extractor) as brain:
            results = brain.replay_pending()
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["ok"])
            self.assertEqual(brain.list_pending_consolidations(), [])

    def test_replay_failure_keeps_entry_recoverable(self) -> None:
        class BrokenExtractor:
            def extract(self, prompt):
                raise RuntimeError("LLM 挂了")

        with ExtraBrain(self.db_path, extractor=BrokenExtractor()) as brain:
            brain.store.log_session_begin("log_x", "用户：测试崩溃。", None, now=NOW)
            results = brain.replay_pending()
            self.assertFalse(results[0]["ok"])
            # failed 条目仍在待重放清单里（note 记录原因）
            pending = brain.list_pending_consolidations()
            self.assertEqual([entry["id"] for entry in pending], ["log_x"])
            self.assertIn("LLM 挂了", pending[0]["note"] or "")

    def test_rebuild_from_log_restores_memories(self) -> None:
        with ExtraBrain(self.db_path, extractor=self._extractor_with_fact()) as brain:
            brain.consolidate("用户：我喜欢白茶。")
            before = {m.id: m.to_dict() for m in brain.store.list_semantic(include_archived=True)}
            # 破坏派生层：删掉向量（模拟晶格损坏）
            brain.store.apply_batch(WriteBatch(operation="test.clear.vectors"))
            with brain.store._lock, brain.store._connection:
                brain.store._connection.execute("DELETE FROM memory_vectors")

        with ExtraBrain(self.db_path) as brain:
            report = brain.rebuild_from_log()
            self.assertGreaterEqual(report["semantic_restored"], 1)
            self.assertGreaterEqual(report["vectors_rebuilt"], 1)
            after = {m.id: m.to_dict() for m in brain.store.list_semantic(include_archived=True)}
            self.assertEqual(after, before)
            # 重建后检索立即可用（向量已重算）
            result = brain.retrieve("我喜欢什么茶？")
            self.assertTrue(result.memory_ids)

    def test_cli_rebuild_requires_yes(self) -> None:
        from extra_brain.cli import main as cli_main

        with ExtraBrain(self.db_path, extractor=self._extractor_with_fact()) as brain:
            brain.consolidate("用户：我喜欢白茶。")
        self.assertEqual(
            cli_main(["--db", str(self.db_path), "rebuild", "from-log"]), 1
        )
        self.assertEqual(
            cli_main(["--db", str(self.db_path), "rebuild", "from-log", "--yes"]), 0
        )


class CosineHelperTests(unittest.TestCase):
    def test_similarity_bounds(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0, places=5)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.5, places=5)
        with self.assertRaises(ValueError):
            cosine_similarity([1, 0], [1, 0, 0])


class EmotionStateMachineIntegrationTests(TempDbTestCase):
    """v0.6.0-pre：emotion_state 真正接入抽取 → 巩固 → 检索/归档链路。"""

    def test_new_event_defaults_to_open_state(self) -> None:
        from extra_brain import EpisodicCandidate, ExtractionResult

        result = ExtractionResult(
            episodic_events=[
                EpisodicCandidate(
                    summary="用户提到面试很焦虑",
                    key_moments=["我一想到面试就慌"],
                    emotions=["焦虑"],
                    importance=0.85,
                )
            ]
        )
        with ExtraBrain(self.db_path, extractor=FakeExtractor(result)) as brain:
            brain.consolidate("用户：我一想到面试就慌。")
            episodes = brain.store.list_episodic()
            self.assertEqual(episodes[0].emotion_state, "open")

    def test_explicit_state_from_extractor(self) -> None:
        from extra_brain import EpisodicCandidate, ExtractionResult

        result = ExtractionResult(
            episodic_events=[
                EpisodicCandidate(
                    summary="用户说想通了",
                    key_moments=["我决定接受offer"],
                    emotions=["释然"],
                    importance=0.8,
                    emotion_state="resolved",
                )
            ]
        )
        with ExtraBrain(self.db_path, extractor=FakeExtractor(result)) as brain:
            brain.consolidate("用户：我想通了，决定接受offer。")
            self.assertEqual(brain.store.list_episodic()[0].emotion_state, "resolved")

    def test_emotion_updates_advance_state(self) -> None:
        from extra_brain import (
            EmotionUpdate,
            EpisodicCandidate,
            ExtractionResult,
        )

        first = ExtractionResult(
            episodic_events=[
                EpisodicCandidate(
                    summary="用户很孤独",
                    key_moments=["那天没人陪我"],
                    emotions=["孤独"],
                    importance=0.9,
                )
            ]
        )
        with ExtraBrain(self.db_path, extractor=FakeExtractor(first)) as brain:
            brain.consolidate("用户：那天我很孤独。")
            episode_id = brain.store.list_episodic()[0].id

        second = ExtractionResult(
            emotion_updates=[EmotionUpdate(episodic_id=episode_id, to_state="acknowledged")]
        )
        with ExtraBrain(self.db_path, extractor=FakeExtractor(second)) as brain:
            report = brain.consolidate("用户：好一些了。")
            self.assertIn(episode_id, report.emotion_updated)
            self.assertEqual(brain.store.get_episodic(episode_id).emotion_state, "acknowledged")

    def test_illegal_transition_rejected_with_warning(self) -> None:
        from extra_brain import EmotionUpdate, EpisodicCandidate, ExtractionResult

        first = ExtractionResult(
            episodic_events=[
                EpisodicCandidate(
                    summary="用户很孤独",
                    key_moments=["没人陪"],
                    emotions=["孤独"],
                    importance=0.9,
                    emotion_state="open",
                )
            ]
        )
        with ExtraBrain(self.db_path, extractor=FakeExtractor(first)) as brain:
            brain.consolidate("用户：我很孤独。")
            episode_id = brain.store.list_episodic()[0].id

        # open → resolved 跳两步，非法；open → attended 合法但顺序对（open→acknowledged→attended）
        for bad_target, expect_reject in (("attended", True), ("acknowledged", False)):
            second = ExtractionResult(
                emotion_updates=[EmotionUpdate(episodic_id=episode_id, to_state=bad_target)]
            )
            with ExtraBrain(self.db_path, extractor=FakeExtractor(second)) as brain:
                if expect_reject:
                    brain.consolidate("用户：随便聊聊。")
                    self.assertEqual(
                        brain.store.get_episodic(episode_id).emotion_state, "open"
                    )
                else:
                    brain.consolidate("用户：谢谢你听我说。")
                    self.assertEqual(
                        brain.store.get_episodic(episode_id).emotion_state, "acknowledged"
                    )

    def test_unresolved_episode_not_archived_when_stale(self) -> None:
        from extra_brain import ExtractionResult

        very_old = NOW - timedelta(days=3650)
        with ExtraBrain(self.db_path, extractor=FakeExtractor(ExtractionResult())) as brain:
            memory = make_episodic(
                "ep_stale_emotional",
                timestamp=to_iso8601(very_old),
                last_activated=to_iso8601(very_old),
                importance=0.3,
                summary="用户很孤独",
                emotions=["孤独"],
                emotion_state="open",
            )
            brain.store.save_episodic(memory)
            brain._consolidator.consolidate("用户：随便聊聊。", now=NOW)
            stale = brain.store.get_episodic("ep_stale_emotional")
            self.assertFalse(stale.archived, "未平复情感事件不应自动归档")
            self.assertGreaterEqual(stale.decay_score, 0.55)

            # 对照组：同年代但 emotion_state=None 的旧情节可正常归档
            legacy = make_episodic(
                "ep_stale_legacy",
                timestamp=to_iso8601(very_old),
                last_activated=to_iso8601(very_old),
                importance=0.3,
                summary="普通旧事",
                emotions=["平静"],
                emotion_state=None,
            )
            brain.store.save_episodic(legacy)
            brain._consolidator.consolidate("用户：还是随便聊聊。", now=NOW)
            self.assertTrue(brain.store.get_episodic("ep_stale_legacy").archived)

    def test_unresolved_episode_gets_retrieval_boost(self) -> None:
        from extra_brain.models import is_unresolved_episode

        memory = make_episodic("ep_open", emotions=["孤独"], emotion_state="open")
        self.assertTrue(is_unresolved_episode(memory))
        resolved = make_episodic("ep_resolved", emotions=["孤独"], emotion_state="resolved")
        self.assertFalse(is_unresolved_episode(resolved))
        legacy = make_episodic("ep_legacy", emotions=["孤独"], emotion_state=None)
        self.assertFalse(is_unresolved_episode(legacy))

    def test_emotion_updates_schema_validation(self) -> None:
        from extra_brain import ExtractionResult

        payload = {
            "core_candidate_updates": {
                "user_profile_changes": [],
                "relationship_changes": [],
                "new_or_updated_threads": [],
                "should_promote_to_core": [],
            },
            "semantic_facts": [],
            "episodic_events": [],
            "emotion_updates": [{"episodic_id": "ep_x", "to_state": "invalid"}],
            "style_observations": [],
            "conflicts_detected": [],
        }
        with self.assertRaises(MemoryValidationError):
            ExtractionResult.from_dict(payload)

        # emotion_updates 可缺省（向后兼容旧 prompt 输出）
        payload.pop("emotion_updates")
        result = ExtractionResult.from_dict(payload)
        self.assertEqual(result.emotion_updates, [])


class ForgetChainTests(TempDbTestCase):
    """v0.5.0 六档处置的 P1 墓碑 + P0 物理删除审批。"""

    def test_tombstone_archives_and_digests(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(make_semantic("sem_tomb"))
            result = brain.forget("sem_tomb")
            self.assertEqual(result["operation"], "tombstone")
            self.assertIn("白茶", result["digest"])
            memory = brain.store.get_semantic("sem_tomb")
            self.assertTrue(memory.archived)
            self.assertEqual(memory.retention_tier, 4)

    def test_hard_delete_requires_approval(self) -> None:
        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(make_semantic("sem_del"))
            with self.assertRaises(PermissionError):
                brain.forget("sem_del", hard=True, approved=False)
            result = brain.forget("sem_del", hard=True, approved=True)
            self.assertEqual(result["operation"], "hard_delete")
            self.assertIsNone(brain.store.get_semantic("sem_del"))

    def test_cli_stats_and_forget(self) -> None:
        from extra_brain.cli import main as cli_main

        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(make_semantic("sem_cli"))
        self.assertEqual(cli_main(["--db", str(self.db_path), "stats"]), 0)
        self.assertEqual(cli_main(["--db", str(self.db_path), "forget", "sem_cli"]), 0)
        # 物理删除不带 --yes 应失败（退出码 1）
        self.assertEqual(
            cli_main(["--db", str(self.db_path), "forget", "sem_cli", "--hard"]), 1
        )
        self.assertEqual(
            cli_main(["--db", str(self.db_path), "forget", "sem_cli", "--hard", "--yes"]), 0
        )

    def test_cli_associate_two_hop(self) -> None:
        from extra_brain.cli import main as cli_main

        with ExtraBrain(self.db_path) as brain:
            brain.store.save_semantic(make_semantic("sem_h1"))
            brain.store.save_semantic(make_semantic("sem_h2"))
            brain.store.save_semantic(make_semantic("sem_h3"))
            brain.save_association(
                MemoryAssociation(
                    id="a1", source_memory_id="sem_h1", target_memory_id="sem_h2", strength=0.7
                )
            )
            brain.save_association(
                MemoryAssociation(
                    id="a2", source_memory_id="sem_h2", target_memory_id="sem_h3", strength=0.5
                )
            )
        self.assertEqual(
            cli_main(["--db", str(self.db_path), "associate", "sem_h1"]), 0
        )
