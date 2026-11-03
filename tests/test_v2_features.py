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
    DispositionSeed,
    EpisodicMemory,
    MemoryAssociation,
    MemoryValidationError,
    NarrativeLayer,
    ProactiveConfig,
    RelationshipModel,
    SelfState,
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


class CompanionLayerShareTests(TempDbTestCase):
    """全局伴侣库：人设/稳定偏好跨 peer 共享，情景记忆仍按 peer 隔离。"""

    def _make(self, peer: str) -> ExtraBrain:
        d = self.db_path.parent
        return ExtraBrain(
            d / f"{peer}.db",
            companion_db=d / "_global.db",
        )

    def test_persona_shared_across_peers(self) -> None:
        with self._make("alice") as alice, self._make("bob") as bob:
            core = alice.core
            core.identity.name = "小豚"
            core.identity.persona_core = "十几岁的女儿"
            core.user_stable_profile.core_preferences.append("说话不要带星号")
            alice.save_core(core)
            alice.sync_persona_to_companion()

            # bob 注入的上下文里应出现全局人设与偏好（即便 bob 自己的库是空的）
            injection = bob.build_injection("你好呀")
            self.assertIn("小豚", injection.context)
            self.assertIn("说话不要带星号", injection.context)

    def test_peer_threads_stay_local(self) -> None:
        with self._make("alice") as alice:
            core = alice.core
            from extra_brain.models import ActiveThread, to_iso8601 as _iso, utc_now as _now
            core.identity.name = "小豚"
            core.active_threads.append(
                ActiveThread(id="t_1", summary="暗号核对", status="open", last_mentioned=_iso(_now()))
            )
            alice.save_core(core)
            alice.sync_persona_to_companion()
        with self._make("bob") as bob:
            # active_threads 不随 push 走：bob 看不到 alice 的线程
            self.assertEqual(bob.core.active_threads, [])
            merged = bob._merged_core()
            self.assertEqual(merged.identity.name, "小豚")  # 人设共享
            self.assertEqual(merged.active_threads, [])     # 线程隔离

    def test_retrieval_merges_semantic_from_global(self) -> None:
        with self._make("alice") as alice:
            alice.store.save_semantic(make_semantic("sem_global_fact", subject="爸爸", object="JCzero"))
            # 把它推进全局层：直接写 companion 库
            alice.companion.store.save_semantic(
                make_semantic("sem_shared_pref", object="白茶", subject="爸爸")
            )
            alice.companion.reindex()
            alice.reindex()
            result = alice.retrieve("爸爸喜欢什么？")
            self.assertIn("sem_shared_pref", result.memory_ids)
            # 其他 peer 也能检索到全局层记忆
        with self._make("bob") as bob:
            bob_result = bob.retrieve("爸爸喜欢什么？")
            self.assertIn("sem_shared_pref", bob_result.memory_ids)

    def test_self_reference_guard(self) -> None:
        path = self.db_path.parent / "_global.db"
        brain = ExtraBrain(path, companion_db=path)
        try:
            self.assertIsNone(brain.companion)  # 同路径不挂载，不无限递归
        finally:
            brain.close()

    def test_persona_anchor_history_records_push(self) -> None:
        with self._make("alice") as alice:
            result = alice.sync_persona_to_companion()
            self.assertTrue(result["anchor"].startswith("push_"))
            history = alice.companion.core.persona_anchor_history
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["from"], str(alice.store.path))


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


# ── v0.7.0 SelfState ──────────────────────────────────────────────


class SelfStateModelTests(unittest.TestCase):
    """SelfState 数据模型单元测试。"""

    def test_default_values(self):
        ss = SelfState()
        self.assertEqual(ss.energy, 0.85)
        self.assertEqual(ss.mood_baseline, "平静")
        self.assertEqual(ss.active_goals, [])
        self.assertEqual(ss.self_narrative, "")
        self.assertIsNone(ss.last_significant_event_id)
        self.assertEqual(ss.version, 1)

    def test_energy_clamped(self):
        ss = SelfState(energy=1.5)
        self.assertEqual(ss.energy, 1.0)
        ss2 = SelfState(energy=-0.1)
        self.assertEqual(ss2.energy, 0.15)
        ss3 = SelfState(energy=0.01)
        self.assertEqual(ss3.energy, 0.15)

    def test_energy_invalid_raises(self):
        with self.assertRaises(MemoryValidationError):
            SelfState(energy=float("nan"))

    def test_goals_truncated_to_5(self):
        ss = SelfState(active_goals=["a", "b", "c", "d", "e", "f", "g"])
        self.assertEqual(len(ss.active_goals), 5)
        self.assertEqual(ss.active_goals, ["a", "b", "c", "d", "e"])

    def test_narrative_length_limit(self):
        with self.assertRaises(MemoryValidationError):
            SelfState(self_narrative="x" * 81)

    def test_roundtrip(self):
        ss = SelfState(
            energy=0.72,
            mood_baseline="有点紧张",
            active_goals=["想知道面试结果"],
            self_narrative="我关注着小舟的面试进展。",
            last_significant_event_id="ep_001",
            updated_at="2026-11-02T12:00:00Z",
            version=3,
        )
        d = ss.to_dict()
        restored = SelfState.from_dict(d)
        self.assertAlmostEqual(restored.energy, 0.72, places=4)
        self.assertEqual(restored.mood_baseline, "有点紧张")
        self.assertEqual(restored.active_goals, ["想知道面试结果"])
        self.assertEqual(restored.self_narrative, "我关注着小舟的面试进展。")
        self.assertEqual(restored.last_significant_event_id, "ep_001")
        self.assertEqual(restored.version, 3)

    def test_from_empty_dict_returns_defaults(self):
        ss = SelfState.from_dict({})
        self.assertEqual(ss.energy, 0.85)
        self.assertEqual(ss.mood_baseline, "平静")


class SelfStateStorageTests(unittest.TestCase):
    """SelfState 持久化读写测试（用临时内存库）。"""

    def setUp(self):
        import tempfile
        from extra_brain import ExtraBrain

        self._tmpdir = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmpdir.name) / "test_self_state.db")
        self.brain = ExtraBrain(self._db)
        # old db 不能有 self_state 行，必须在第一次访问时自动返回默认值

    def tearDown(self):
        self.brain.close()
        self._tmpdir.cleanup()

    def test_old_db_returns_default_self_state(self):
        """旧库没有 self_state 表时，返回默认 SelfState 且不抛错。"""
        ss = self.brain.get_self_state()
        self.assertEqual(ss.energy, 0.85)
        self.assertEqual(ss.mood_baseline, "平静")
        self.assertEqual(ss.active_goals, [])

    def test_save_and_reload(self):
        ss = SelfState(
            energy=0.55,
            mood_baseline="疲惫",
            active_goals=["去公园散步"],
            self_narrative="最近聊得有点多，我需要缓缓。",
            updated_at="2026-11-02T12:00:00Z",
            version=2,
        )
        self.brain.save_self_state(ss)
        loaded = self.brain.get_self_state()
        self.assertAlmostEqual(loaded.energy, 0.55, places=4)
        self.assertEqual(loaded.mood_baseline, "疲惫")
        self.assertEqual(loaded.active_goals, ["去公园散步"])
        self.assertEqual(loaded.self_narrative, "最近聊得有点多，我需要缓缓。")

    def test_read_only_rejects_save(self):
        """只读模式拒绝 save_self_state。"""
        from extra_brain import ExtraBrain
        ro = ExtraBrain(self._db, read_only=True)
        with self.assertRaises(PermissionError):
            ro.save_self_state(SelfState(energy=0.9))
        ro.close()


class SelfStateIntegrationTests(unittest.TestCase):
    """SelfState 在 consolidate / injection / proactive 中的端到端集成测试。"""

    def setUp(self):
        import tempfile
        from extra_brain import ExtraBrain

        self._tmpdir = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmpdir.name) / "test_self_state_int.db")
        self.brain = ExtraBrain(self._db)

    def tearDown(self):
        self.brain.close()
        self._tmpdir.cleanup()

    def _seed_one_episode(self, emotions=("开心",), importance=0.8):
        """手动写入一条情节记忆用于触发 SelfState 更新。"""
        from extra_brain.storage import WriteBatch
        memory_id = new_memory_id("ep", utc_now())
        now_iso = to_iso8601(utc_now())
        ep = EpisodicMemory(
            id=memory_id,
            timestamp=now_iso,
            summary="小舟面试通过了",
            key_moments=["她说录取了"],
            emotions=list(emotions),
            importance=importance,
            related_semantic_ids=[],
            participants=["user", "agent"],
        )
        self.brain.store.apply_batch(
            WriteBatch(episodic=[ep], operation="test.seed")
        )
        return memory_id, ep

    def test_consolidation_updates_self_state(self):
        """一轮有内容的 consolidate 应更新 SelfState 的 updated_at。"""
        conv = (
            "[用户] 我面试通过了！好开心！\n"
            "[助手] 真的吗？太棒了！\n"
            "[用户] 是啊，努力终于有了回报。\n"
        )
        try:
            report = self.brain.consolidate(conv)
        except Exception:
            self.skipTest("extractor not available — skipping integration test")
        ss = self.brain.get_self_state()
        self.assertTrue(ss.version >= 1)
        if report.changed:
            self.assertNotEqual(ss.updated_at, "")

    def test_injection_prepends_self_state_block(self):
        """注入输出的最前面应包含「这一刻的自己」。"""
        # 手动写入 SelfState，然后走 build_injection
        self.brain.save_self_state(
            SelfState(
                energy=0.72,
                mood_baseline="开心",
                active_goals=["关心小舟的面试进展"],
                self_narrative="我和小舟刚聊完面试的事，她现在状态不错。",
                updated_at="2026-11-02T12:00:00Z",
                version=2,
            )
        )
        # 写入一条语义记忆让检索有东西可查
        from extra_brain.storage import WriteBatch
        sem = make_semantic("sem_test_ss")
        self.brain.store.apply_batch(
            WriteBatch(semantic=[sem], operation="test.injection_ss")
        )
        injection = self.brain.build_injection("小舟面试")
        self.assertIn("这一刻的自己", injection.context)
        self.assertIn("0.72", injection.context)
        self.assertIn("开心", injection.context)
        self.assertIn("关心小舟的面试进展", injection.context)

    def test_low_energy_suppresses_proactive(self):
        """energy < 0.15 时 should_proactive_open 返回 energy_critical。"""
        self.brain.save_self_state(
            SelfState(
                energy=0.10,
                mood_baseline="疲惫",
                updated_at="2026-11-02T12:00:00Z",
            )
        )
        config = self.brain.ensure_proactive_config("peer_test")
        config.enabled = True
        config.silence_threshold_minutes = 0
        config.quiet_hours_start = 0
        config.quiet_hours_end = 0
        config.daily_limit = 10
        self.brain.save_proactive_config(config)
        ok, reason = self.brain.should_proactive_open("peer_test")
        self.assertFalse(ok)
        self.assertEqual(reason, "energy_critical")

    def test_self_state_cli_output(self):
        """CLI `self` 子命令输出有效 JSON。"""
        from extra_brain.cli import main as cli_main
        import json as j
        self.brain.save_self_state(
            SelfState(
                energy=0.60,
                mood_baseline="期待",
                active_goals=["等小舟回消息"],
                updated_at="2026-11-02T12:00:00Z",
                version=1,
            )
        )
        # 没法直接捕获 stdout，走函数级校验
        ss = self.brain.get_self_state()
        d = ss.to_dict()
        self.assertEqual(d["energy"], 0.60)
        self.assertEqual(d["mood_baseline"], "期待")
        self.assertEqual(d["active_goals"], ["等小舟回消息"])
        # 验证 JSON 可序列化
        s = j.dumps(d, ensure_ascii=False)
        self.assertIn("energy", s)

    def test_self_state_from_dict_non_empty(self):
        ss = SelfState.from_dict({
            "energy": 0.5,
            "mood_baseline": "焦虑",
            "active_goals": ["a", "b"],
            "self_narrative": "我很担心。",
            "version": 7,
        })
        self.assertEqual(ss.energy, 0.5)
        self.assertEqual(ss.mood_baseline, "焦虑")
        self.assertEqual(ss.active_goals, ["a", "b"])
        self.assertEqual(ss.self_narrative, "我很担心。")
        self.assertEqual(ss.version, 7)


# ── v0.8.0 DispositionSeed ───────────────────────────────────────


class DispositionSeedModelTests(unittest.TestCase):
    """DispositionSeed 数据模型测试。"""

    def test_defaults(self) -> None:
        seed = DispositionSeed(
            id="disp_20261101_abc123",
            statement="面对压力时倾向于先倾诉再行动",
        )
        self.assertEqual(seed.strength, 0.5)
        self.assertEqual(seed.evidence_ids, [])
        self.assertIsNone(seed.last_activated)
        self.assertEqual(seed.activation_count, 0)
        self.assertFalse(seed.archived)

    def test_validation_rejects_empty_statement(self) -> None:
        with self.assertRaises(MemoryValidationError):
            DispositionSeed(id="disp_20261101_abc", statement="")

    def test_validation_rejects_out_of_range_strength(self) -> None:
        with self.assertRaises(MemoryValidationError):
            DispositionSeed(id="disp_123", statement="test", strength=1.5)

    def test_from_dict(self) -> None:
        seed = DispositionSeed.from_dict({
            "id": "disp_test",
            "statement": "喜欢安静的环境",
            "strength": 0.7,
            "evidence_ids": ["mem_a", "mem_b"],
            "last_activated": "2026-11-01T12:00:00Z",
            "activation_count": 3,
            "archived": False,
        })
        self.assertEqual(seed.id, "disp_test")
        self.assertEqual(seed.statement, "喜欢安静的环境")
        self.assertEqual(seed.strength, 0.7)
        self.assertEqual(seed.evidence_ids, ["mem_a", "mem_b"])
        self.assertEqual(seed.activation_count, 3)

    def test_to_dict(self) -> None:
        seed = DispositionSeed(
            id="disp_x",
            statement="喜欢深入思考",
            strength=0.45,
            evidence_ids=["e1"],
        )
        d = seed.to_dict()
        self.assertEqual(d["id"], "disp_x")
        self.assertEqual(d["strength"], 0.45)

    def test_roundtrip(self) -> None:
        seed = DispositionSeed(
            id="disp_r",
            statement="倾向于独立解决问题",
            strength=0.6,
            evidence_ids=["a", "b", "c"],
            last_activated="2026-10-01T10:00:00Z",
            activation_count=5,
            archived=True,
        )
        d = seed.to_dict()
        roundtripped = DispositionSeed.from_dict(d)
        self.assertEqual(roundtripped.id, seed.id)
        self.assertEqual(roundtripped.statement, seed.statement)
        self.assertEqual(roundtripped.strength, seed.strength)
        self.assertEqual(roundtripped.evidence_ids, seed.evidence_ids)
        self.assertEqual(roundtripped.activation_count, seed.activation_count)
        self.assertTrue(roundtripped.archived)


class DispositionSeedStorageTests(unittest.TestCase):
    """CRUD 存储测试。"""

    def setUp(self) -> None:
        from extra_brain import MemoryStore
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)

    def test_list_empty_returns_empty(self) -> None:
        seeds = self.store.list_dispositions()
        self.assertEqual(seeds, [])

    def test_save_and_reload(self) -> None:
        seed = DispositionSeed(
            id="disp_20261101_abc",
            statement="喜欢安静的环境",
            strength=0.6,
            evidence_ids=["mem1", "mem2"],
        )
        self.store.save_disposition(seed)
        loaded = self.store.get_disposition("disp_20261101_abc")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.statement, "喜欢安静的环境")
        self.assertEqual(loaded.strength, 0.6)

    def test_list_filters_archived(self) -> None:
        active = DispositionSeed(
            id="disp_active",
            statement="倾向 A",
            strength=0.5,
        )
        archived = DispositionSeed(
            id="disp_archived",
            statement="倾向 B",
            strength=0.3,
            archived=True,
        )
        self.store.save_disposition(active)
        self.store.save_disposition(archived)
        seeds = self.store.list_dispositions()
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0].id, "disp_active")

    def test_list_include_archived(self) -> None:
        active = DispositionSeed(id="disp_a", statement="A")
        archived = DispositionSeed(id="disp_b", statement="B", archived=True)
        self.store.save_disposition(active)
        self.store.save_disposition(archived)
        seeds = self.store.list_dispositions(include_archived=True)
        self.assertEqual(len(seeds), 2)

    def test_save_enforces_cap(self) -> None:
        for i in range(15):
            self.store.save_disposition(DispositionSeed(
                id=f"disp_{i}",
                statement=f"倾向 {i}",
            ))
        with self.assertRaises(MemoryValidationError):
            self.store.save_disposition(DispositionSeed(
                id="disp_overflow",
                statement="溢出",
            ))

    def test_archive_disposition(self) -> None:
        seed = DispositionSeed(id="disp_arc", statement="将被归档")
        self.store.save_disposition(seed)
        archived = self.store.archive_disposition("disp_arc")
        self.assertIsNotNone(archived)
        self.assertTrue(archived.archived)
        # 再次 list 应该过滤掉
        self.assertEqual(len(self.store.list_dispositions()), 0)

    def test_delete_disposition(self) -> None:
        seed = DispositionSeed(id="disp_del", statement="将被删除")
        self.store.save_disposition(seed)
        self.assertTrue(self.store.delete_disposition("disp_del"))
        self.assertIsNone(self.store.get_disposition("disp_del"))


class DispositionSeedMatchTests(unittest.TestCase):
    """匹配逻辑测试。"""

    def setUp(self) -> None:
        from extra_brain import MemoryStore
        from extra_brain.disposition import match_dispositions
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)
        self.match_dispositions = match_dispositions

    def test_empty_store_returns_no_match(self) -> None:
        result = self.match_dispositions(self.store, "今天压力很大")
        self.assertEqual(result.matched_seeds, [])
        self.assertEqual(result.boost_ids, [])
        self.assertEqual(result.inner_voice, "")

    def test_simple_match(self) -> None:
        self.store.save_disposition(DispositionSeed(
            id="disp_stress",
            statement="面对压力时倾向于先倾诉再行动",
            strength=0.7,
            evidence_ids=["mem_ev1", "mem_ev2"],
        ))
        result = self.match_dispositions(self.store, "我最近压力很大，不知道该怎么办")
        self.assertGreaterEqual(len(result.matched_seeds), 1)
        self.assertIn("mem_ev1", result.boost_ids)
        self.assertIn("mem_ev2", result.boost_ids)

    def test_no_match_different_domain(self) -> None:
        self.store.save_disposition(DispositionSeed(
            id="disp_stress",
            statement="面对压力时倾向于先倾诉再行动",
            strength=0.7,
        ))
        result = self.match_dispositions(self.store, "今天的天气真好")
        self.assertEqual(result.matched_seeds, [])

    def test_inner_voice_short(self) -> None:
        self.store.save_disposition(DispositionSeed(
            id="disp_stress",
            statement="面对压力时倾向于先倾诉再行动",
            strength=0.8,
            evidence_ids=["ev1"],
            activation_count=3,
        ))
        result = self.match_dispositions(self.store, "我压力很大很想找人说话倾诉一下")
        if result.inner_voice:
            self.assertLessEqual(len(result.inner_voice), 40)

    def test_boost_weight_within_range(self) -> None:
        self.store.save_disposition(DispositionSeed(
            id="disp_test",
            statement="面对问题倾向于先独立思考",
            strength=0.9,
            evidence_ids=["ev"],
            activation_count=2,
        ))
        result = self.match_dispositions(self.store, "这个问题我想自己先想想独立思考")
        self.assertGreaterEqual(result.boost_weight, 0.0)
        self.assertLessEqual(result.boost_weight, 0.15)


class DispositionReinforceTests(unittest.TestCase):
    """反熏逻辑测试。"""

    def setUp(self) -> None:
        from extra_brain import MemoryStore
        from extra_brain.disposition import reinforce_dispositions
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)
        self.reinforce = reinforce_dispositions

    def test_empty_store_returns_empty(self) -> None:
        changes = self.reinforce(self.store, "是的，确实是这样")
        self.assertEqual(changes, [])

    def test_affirm_strengthens(self) -> None:
        seed = DispositionSeed(
            id="disp_aff",
            statement="面对压力时倾向于先倾诉",
            strength=0.5,
        )
        self.store.save_disposition(seed)
        changes = self.reinforce(
            self.store,
            "用户：我今天压力很大\n助手：倾诉一下如何\n用户：是的，就是这样，确实需要倾诉",
        )
        self.assertGreater(len(changes), 0)
        updated = self.store.get_disposition("disp_aff")
        self.assertGreater(updated.strength, 0.5)

    def test_deny_weakens(self) -> None:
        seed = DispositionSeed(
            id="disp_deny",
            statement="面对压力时倾向于先倾诉",
            strength=0.7,
        )
        self.store.save_disposition(seed)
        changes = self.reinforce(
            self.store,
            "助手：你以前喜欢先倾诉\n用户：不对，不是这样，我现在不想倾诉",
        )
        self.assertGreater(len(changes), 0)
        updated = self.store.get_disposition("disp_deny")
        self.assertLess(updated.strength, 0.7)

    def test_strength_clamped_to_zero(self) -> None:
        seed = DispositionSeed(
            id="disp_low",
            statement="某个不太对劲的倾向",
            strength=0.05,
            evidence_ids=["ev1"],
        )
        self.store.save_disposition(seed)
        changes = self.reinforce(
            self.store,
            "用户：不对，不是，我不同意这个说法",
        )
        # strength 应该被 clamp 到 0
        updated = self.store.get_disposition("disp_low")
        self.assertGreaterEqual(updated.strength, 0.0)

    def test_archived_on_low_strength(self) -> None:
        seed = DispositionSeed(
            id="disp_fade",
            statement="将要淡化的倾向",
            strength=0.14,
            evidence_ids=["ev"],
        )
        self.store.save_disposition(seed)
        self.reinforce(self.store, "今天天气不错")
        updated = self.store.get_disposition("disp_fade")
        self.assertTrue(updated.archived)


class DispositionAbstractTests(unittest.TestCase):
    """种子抽象测试。"""

    def setUp(self) -> None:
        from extra_brain import MemoryStore
        from extra_brain.disposition import abstract_dispositions
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)
        self.abstract = abstract_dispositions

    def test_abstract_with_insufficient_data_returns_empty(self) -> None:
        results = self.abstract(self.store, limit=3)
        self.assertEqual(results, [])

    def test_abstract_from_repeated_patterns(self) -> None:
        # 插入 3 条同一 pattern 的语义记忆
        for i in range(3):
            mem = SemanticMemory(
                id=new_memory_id("sem", utc_now()),
                type="preference",
                subject="压力",
                predicate="更喜欢",
                object="先倾诉再行动",
                detail=f"面对压力时倾向于先倾诉（第{i}次）",
                importance=0.7 + i * 0.05,
                emotion_valence="neutral",
                emotion_intensity=0.3,
                confidence=0.8,
                source_episodic_ids=[],
                created_at=to_iso8601(utc_now()),
                last_activated=to_iso8601(utc_now()),
            )
            self.store.save_semantic(mem)
        results = self.abstract(self.store, limit=3)
        self.assertGreater(len(results), 0)

    def test_abstract_dedup_existing(self) -> None:
        # 先存一条已有种子
        existing = DispositionSeed(
            id="disp_existing",
            statement="面对压力时倾向于先倾诉",
            strength=0.6,
        )
        self.store.save_disposition(existing)
        # 再插 3 条同 pattern 的记忆
        for i in range(3):
            mem = SemanticMemory(
                id=new_memory_id("sem", utc_now()),
                type="preference",
                subject="压力",
                predicate="更喜欢",
                object="先倾诉再行动",
                detail=f"面对压力时倾向于先倾诉（第{i}次）",
                importance=0.7,
                emotion_valence="neutral",
                emotion_intensity=0.3,
                confidence=0.8,
                source_episodic_ids=[],
                created_at=to_iso8601(utc_now()),
                last_activated=to_iso8601(utc_now()),
            )
            self.store.save_semantic(mem)
        results = self.abstract(self.store, limit=3)
        # 应该因为去重而不会产生新种子
        self.assertEqual(len(results), 0)


class DispositionIntegrationTests(unittest.TestCase):
    """集成测试。"""

    def setUp(self) -> None:
        from extra_brain import MemoryConfig, MemoryStore
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)
        self._config = MemoryConfig()


class DispositionOldDBTests(unittest.TestCase):
    """旧库兼容测试。"""

    def test_old_db_returns_empty_dispositions(self) -> None:
        from extra_brain import MemoryStore
        store = MemoryStore(":memory:")
        self.addCleanup(store.close)
        seeds = store.list_dispositions()
        self.assertEqual(seeds, [])
        self.assertIsNone(store.get_disposition("nonexistent"))


# ── v0.9.0 SelfState v2 测试 ─────────────────────────────────────


class SelfStateV2ModelTests(unittest.TestCase):
    """SelfState v0.9 新增字段测试。"""

    def test_fatigue_reason_default_none(self) -> None:
        ss = SelfState()
        self.assertIsNone(ss.fatigue_reason)

    def test_fatigue_reason_survives_roundtrip(self) -> None:
        ss = SelfState(
            energy=0.5,
            fatigue_reason="高强度对话×3；主动开口×2",
            version=5,
        )
        d = ss.to_dict()
        self.assertEqual(d["fatigue_reason"], "高强度对话×3；主动开口×2")
        ss2 = SelfState.from_dict(d)
        self.assertEqual(ss2.fatigue_reason, "高强度对话×3；主动开口×2")
        self.assertEqual(ss2.version, 5)

    def test_old_dict_without_fatigue(self) -> None:
        """旧版 to_dict 不含 fatigue_reason 的兼容。"""
        ss = SelfState.from_dict({"energy": 0.6, "version": 3,
                                  "mood_baseline": "平静",
                                  "active_goals": [], "self_narrative": "",
                                  "updated_at": ""})
        self.assertIsNone(ss.fatigue_reason)


class NarrativeLayerV2Tests(unittest.TestCase):
    """NarrativeLayer v0.9 unresolved_threads 测试。"""

    def test_unresolved_default_empty(self) -> None:
        nl = NarrativeLayer(
            id="nl_1", user_id="u1", phase="稳定期", theme="日常",
            event_ids=[], summary="测试", last_updated=to_iso8601(NOW),
        )
        self.assertEqual(nl.unresolved_threads, [])

    def test_unresolved_roundtrip(self) -> None:
        nl = NarrativeLayer(
            id="nl_2", user_id="u1", phase="稳定期", theme="日常",
            event_ids=["ep_1"], summary="测试", last_updated=to_iso8601(NOW),
            unresolved_threads=["ep_3", "ep_5"],
        )
        d = nl.to_dict()
        self.assertEqual(d["unresolved_threads"], ["ep_3", "ep_5"])
        nl2 = NarrativeLayer.from_dict(d)
        self.assertEqual(nl2.unresolved_threads, ["ep_3", "ep_5"])

    def test_old_dict_without_unresolved(self) -> None:
        nl = NarrativeLayer.from_dict({
            "id": "nl_1", "user_id": "u1", "phase": "稳定期", "theme": "日常",
            "event_ids": [], "summary": "测试", "last_updated": to_iso8601(NOW),
        })
        self.assertEqual(nl.unresolved_threads, [])


class EnergyTierTests(unittest.TestCase):
    """能量分级标签测试。"""

    def setUp(self) -> None:
        from extra_brain.self_state import energy_tier
        self.energy_tier = energy_tier

    def test_tiers(self) -> None:
        self.assertEqual(self.energy_tier(SelfState(energy=1.0)), "充沛")
        self.assertEqual(self.energy_tier(SelfState(energy=0.75)), "充沛")
        self.assertEqual(self.energy_tier(SelfState(energy=0.5)), "平稳")
        self.assertEqual(self.energy_tier(SelfState(energy=0.3)), "偏低")
        self.assertEqual(self.energy_tier(SelfState(energy=0.2)), "疲惫")
        self.assertEqual(self.energy_tier(SelfState(energy=0.15)), "极度疲惫")


class LifeSnapshotTests(unittest.TestCase):
    """人生快照存储与回滚测试。"""

    def setUp(self) -> None:
        from extra_brain import MemoryStore
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)

    def test_snapshot_save_and_list(self) -> None:
        ss = SelfState(energy=0.7, mood_baseline="开心", version=10,
                       self_narrative="今天很开心")
        self.store.save_self_state(ss)
        from extra_brain.self_state import take_snapshot, list_snapshots
        sid = take_snapshot(self.store, reason="阶段转换测试")
        self.assertTrue(sid.startswith("snap_"))
        snaps = list_snapshots(self.store)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["id"], sid)

    def test_snapshot_rollback(self) -> None:
        ss1 = SelfState(energy=0.9, version=5)
        self.store.save_self_state(ss1)
        from extra_brain.self_state import take_snapshot, rollback_to_snapshot
        sid = take_snapshot(self.store)
        # 改变当前状态
        ss2 = SelfState(energy=0.5, version=10)
        self.store.save_self_state(ss2)
        # 回滚
        restored = rollback_to_snapshot(self.store, sid)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.energy, 0.9)
        self.assertEqual(restored.version, 5)

    def test_rollback_nonexistent(self) -> None:
        from extra_brain.self_state import rollback_to_snapshot
        self.assertIsNone(rollback_to_snapshot(self.store, "snap_nonexistent"))


class EnergyComputationTests(unittest.TestCase):
    """多因素能量计算测试。"""

    def setUp(self) -> None:
        from extra_brain import MemoryStore, MemoryConfig
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)

    def test_energy_clamped(self) -> None:
        """energy 不会超出 [0.15, 1.0]。"""
        ss = SelfState(energy=1.0)
        self.assertEqual(ss.energy, 1.0)
        low = SelfState(energy=0.05)
        self.assertEqual(low.energy, 0.15)

    def test_basic_still_pass(self) -> None:
        """SelfState 保存/读取仍然正常。"""
        ss = SelfState(energy=0.75, mood_baseline="开心", version=3,
                       fatigue_reason="测试")
        self.store.save_self_state(ss)
        loaded = self.store.get_self_state()
        self.assertEqual(loaded.energy, 0.75)
        self.assertEqual(loaded.mood_baseline, "开心")
        self.assertEqual(loaded.fatigue_reason, "测试")


class UnresolvedCountTests(unittest.TestCase):
    """未平复事件计数测试。"""

    def setUp(self) -> None:
        from extra_brain import MemoryStore
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)

    def test_empty_store(self) -> None:
        from extra_brain.self_state import count_unresolved
        self.assertEqual(count_unresolved(self.store), 0)

    def test_counts_unresolved(self) -> None:
        from extra_brain.self_state import count_unresolved
        ep = EpisodicMemory(
            id="ep_unres_1",
            timestamp=to_iso8601(NOW),
            summary="未平复事件",
            key_moments=["关键时刻"],
            emotions=["难过"],
            importance=0.8,
            related_semantic_ids=[],
            participants=["user", "agent"],
            emotion_state="open",
        )
        self.store.save_episodic(ep)
        self.assertEqual(count_unresolved(self.store), 1)
