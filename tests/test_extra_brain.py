from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extra_brain import (  # noqa: E402
    ActiveThread,
    ConversationMessage,
    CoreMemory,
    CoreMemoryLimitError,
    EpisodicMemory,
    ExtraBrain,
    ExtractionResult,
    Identity,
    LocalHashingEmbedder,
    MemoryConfig,
    MemoryValidationError,
    SemanticMemory,
    SessionTracker,
    StyleMemory,
    calculate_decay,
)
from extra_brain.consolidation import Consolidator  # noqa: E402
from extra_brain.extraction import (  # noqa: E402
    ConflictDetection,
    CoreCandidateUpdates,
    EpisodicCandidate,
    ProfileChange,
    SemanticCandidate,
    StyleCandidate,
    ThreadChange,
)
from extra_brain.models import estimate_tokens, to_iso8601  # noqa: E402
from extra_brain.retrieval import RetrievalResult  # noqa: E402


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class FakeExtractor:
    def __init__(self, *results: ExtractionResult) -> None:
        self.results = list(results)
        self.prompts: list[str] = []

    def extract(self, prompt: str) -> ExtractionResult:
        self.prompts.append(prompt)
        if not self.results:
            return ExtractionResult()
        return self.results.pop(0)


def fact(
    *,
    subject: str = "用户",
    predicate: str = "喜欢",
    object: str = "白茶",
    kind: str = "preference",
    importance: float = 0.75,
    confidence: float = 0.9,
    valence: str = "positive",
) -> SemanticCandidate:
    return SemanticCandidate(
        type=kind,
        subject=subject,
        predicate=predicate,
        object=object,
        detail=f"{subject}{predicate}{object}，这是用户明确表达的长期偏好。",
        importance=importance,
        emotion_valence=valence,
        emotion_intensity=0.6,
        confidence=confidence,
    )


def semantic(
    memory_id: str = "sem_20240101_test",
    *,
    timestamp: datetime | None = None,
    importance: float = 0.7,
    protected: bool = False,
    valence: str = "positive",
    intensity: float = 0.7,
) -> SemanticMemory:
    moment = to_iso8601(timestamp or NOW)
    return SemanticMemory(
        id=memory_id,
        type="preference",
        subject="用户",
        predicate="喜欢",
        object="白茶",
        detail="用户说喝白茶会让用户安心。",
        importance=importance,
        emotion_valence=valence,
        emotion_intensity=intensity,
        confidence=0.9,
        source_episodic_ids=[],
        created_at=moment,
        last_activated=moment,
        protected=protected,
    )


def extraction_with_fact(candidate: SemanticCandidate | None = None) -> ExtractionResult:
    return ExtractionResult(semantic_facts=[candidate or fact()])


class BrainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "memory.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def brain(self, *results: ExtractionResult, config: MemoryConfig | None = None) -> ExtraBrain:
        return ExtraBrain(self.db_path, extractor=FakeExtractor(*results), config=config)


class SchemaTests(BrainTestCase):
    def test_original_spec_semantic_json_is_accepted(self) -> None:
        data = semantic().to_dict()
        data.pop("archived")
        restored = SemanticMemory.from_dict(data)
        self.assertFalse(restored.archived)

    def test_extraction_requires_complete_schema_and_valid_ranges(self) -> None:
        empty = {
            "core_candidate_updates": {
                "user_profile_changes": [],
                "relationship_changes": [],
                "new_or_updated_threads": [],
                "should_promote_to_core": [],
            },
            "semantic_facts": [],
            "episodic_events": [],
            "style_observations": [],
            "conflicts_detected": [],
        }
        self.assertEqual(ExtractionResult.from_dict(empty), ExtractionResult())

        malformed = copy.deepcopy(empty)
        del malformed["semantic_facts"]
        with self.assertRaises(MemoryValidationError):
            ExtractionResult.from_dict(malformed)

        malformed = copy.deepcopy(empty)
        malformed["semantic_facts"].append(
            {
                "type": "fact",
                "subject": "用户",
                "predicate": "是",
                "object": "工程师",
                "detail": "明确自述",
                "importance": 2,
                "emotion_valence": "neutral",
                "emotion_intensity": 0,
                "confidence": 0.9,
            }
        )
        with self.assertRaises(MemoryValidationError):
            ExtractionResult.from_dict(malformed)

    def test_empty_thread_id_and_last_mentioned_are_treated_as_omitted(self) -> None:
        data = {
            "core_candidate_updates": {
                "user_profile_changes": [],
                "relationship_changes": [],
                "new_or_updated_threads": [
                    {"id": "", "summary": "准备重要面试", "status": "open", "last_mentioned": ""}
                ],
                "should_promote_to_core": [],
            },
            "semantic_facts": [],
            "episodic_events": [],
            "style_observations": [],
            "conflicts_detected": [],
        }
        result = ExtractionResult.from_dict(data)
        thread = result.core_candidate_updates.new_or_updated_threads[0]
        self.assertIsNone(thread.id)
        self.assertIsNone(thread.last_mentioned)

    def test_persona_and_core_budget_are_enforced(self) -> None:
        with self.assertRaises(MemoryValidationError):
            Identity(persona_core="很" * 151)

        with self.brain() as brain:
            brain.save_core(brain.core)  # 先落库，避免每次 get_core() 生成新默认时间戳
            original = brain.core.to_dict()
            core = brain.core
            core.user_stable_profile.important_life_facts.append("很长" * 1000)
            with self.assertRaises(CoreMemoryLimitError):
                brain.save_core(core)
            self.assertEqual(brain.core.to_dict(), original)

    def test_local_embedding_is_deterministic_and_multilingual(self) -> None:
        embedder = LocalHashingEmbedder(64)
        self.assertEqual(embedder.embed("我喜欢白茶"), embedder.embed("我喜欢白茶"))
        self.assertEqual(len(embedder.embed("white tea")), 64)
        self.assertNotEqual(embedder.embed("我喜欢白茶"), embedder.embed("我讨厌加班"))


class StorageTests(BrainTestCase):
    def test_round_trip_activation_protection_and_audit(self) -> None:
        with self.brain() as brain:
            brain.store.save_semantic(semantic())
            self.assertEqual(brain.store.get_semantic("sem_20240101_test").object, "白茶")

            brain.store.record_activations(["sem_20240101_test"], now=NOW)
            activated = brain.store.get_semantic("sem_20240101_test")
            self.assertEqual(activated.activation_count, 1)

            brain.store.set_protected("sem_20240101_test")
            protected = brain.store.get_semantic("sem_20240101_test")
            self.assertTrue(protected.protected)
            self.assertEqual(protected.decay_score, 1.0)
            self.assertIn("sem_20240101_test", brain.core.protected_memories)

            audit = brain.store.list_audit()
            activation = next(item for item in audit if item["operation"] == "memory.activate.update")
            self.assertEqual(activation["before"]["activation_count"], 0)
            self.assertEqual(activation["after"]["activation_count"], 1)

    def test_persistence_after_reopening(self) -> None:
        with self.brain(extraction_with_fact()) as brain:
            brain.consolidate("用户：我长期喜欢白茶。")
            memory_id = brain.store.list_semantic()[0].id
        with ExtraBrain(self.db_path) as reopened:
            self.assertEqual(reopened.store.get_semantic(memory_id).object, "白茶")


class ConsolidationTests(BrainTestCase):
    def test_full_pipeline_writes_linked_memories_style_core_and_vectors(self) -> None:
        result = ExtractionResult(
            core_candidate_updates=CoreCandidateUpdates(
                user_profile_changes=[
                    ProfileChange("preferred_name", "小胖鱼", "set"),
                    ProfileChange("core_preferences", "安静陪伴", "add"),
                ],
                new_or_updated_threads=[ThreadChange("准备重要面试", "open")],
            ),
            semantic_facts=[fact()],
            episodic_events=[
                EpisodicCandidate(
                    summary="用户在疲惫时分享了白茶带来的安慰。",
                    key_moments=["有你在，我觉得安心。"],
                    emotions=["安心", "感动"],
                    importance=0.9,
                )
            ],
            style_observations=[StyleCandidate("疲惫时先倾听，不急着建议", "interaction_habit", 0.8)],
        )
        extractor = FakeExtractor(result)
        with ExtraBrain(self.db_path, extractor=extractor) as brain:
            report = brain.consolidate(
                [ConversationMessage("user", "我喜欢白茶。有你在，我觉得安心。")],
                session_id="session-1",
            )
            self.assertEqual(report.semantic_written, 1)
            self.assertEqual(report.episodic_written, 1)
            self.assertEqual(report.style_written, 1)
            self.assertEqual(brain.core.user_stable_profile.preferred_name, "小胖鱼")
            self.assertEqual(brain.core.active_threads[0].status, "open")

            stored_semantic = brain.store.list_semantic()[0]
            stored_episode = brain.store.list_episodic()[0]
            self.assertEqual(stored_semantic.source_episodic_ids, [stored_episode.id])
            self.assertEqual(stored_episode.related_semantic_ids, [stored_semantic.id])
            self.assertEqual(stored_episode.raw_reference, "session-1")
            self.assertEqual(len(brain.store.list_style()), 1)
            self.assertIn("对话内容", extractor.prompts[0])
            self.assertIn("我喜欢白茶", extractor.prompts[0])

            retrieved = brain.retrieve("你还记得我喜欢什么茶吗？")
            self.assertEqual([item.memory.id for item in retrieved.semantic], [stored_semantic.id])

    def test_reuse_existing_thread_id_updates_not_fragments(self) -> None:
        first = ExtractionResult(
            core_candidate_updates=CoreCandidateUpdates(
                new_or_updated_threads=[ThreadChange("准备重要面试", "open")]
            )
        )
        with self.brain(first) as brain:
            brain.consolidate("用户：我在准备重要面试。")
            existing_id = brain.core.active_threads[0].id
        second = ExtractionResult(
            core_candidate_updates=CoreCandidateUpdates(
                new_or_updated_threads=[ThreadChange("下周正式面试", "open", id=existing_id)]
            )
        )
        with self.brain(second) as brain:
            brain.consolidate("用户：下周就要正式面试了。")
            self.assertEqual(len(brain.core.active_threads), 1)
            self.assertEqual(brain.core.active_threads[0].id, existing_id)
            self.assertEqual(brain.core.active_threads[0].summary, "下周正式面试")

    def test_fabricated_thread_id_is_replaced_with_generated_id(self) -> None:
        result = ExtractionResult(
            core_candidate_updates=CoreCandidateUpdates(
                new_or_updated_threads=[
                    ThreadChange("准备重要面试", "open", id="fake_thread_id")
                ]
            )
        )
        with self.brain(result) as brain:
            brain.consolidate("用户：我在准备重要面试。")
            thread = brain.core.active_threads[0]
            self.assertNotEqual(thread.id, "fake_thread_id")
            self.assertTrue(thread.id.startswith("thread_"))

    def test_empty_and_low_importance_chatter_do_not_pollute_memory(self) -> None:
        with self.brain(ExtractionResult(), extraction_with_fact(fact(importance=0.05))) as brain:
            first = brain.consolidate("用户：今天云很好看。")
            second = brain.consolidate("用户：随便聊聊。")
            self.assertFalse(first.changed)
            self.assertEqual(brain.store.list_semantic(), [])
            self.assertTrue(second.warnings)

    def test_duplicate_facts_merge_without_creating_another_row(self) -> None:
        first = fact(importance=0.6)
        second = fact(importance=0.85)
        with self.brain(extraction_with_fact(first), extraction_with_fact(second)) as brain:
            brain.consolidate("用户：我喜欢白茶。")
            memory_id = brain.store.list_semantic()[0].id
            brain.consolidate("用户：还是最喜欢白茶。")
            memories = brain.store.list_semantic()
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0].id, memory_id)
            self.assertEqual(memories[0].importance, 0.85)

    def test_newer_higher_confidence_correction_supersedes_old_fact(self) -> None:
        old = fact(predicate="居住城市", object="上海", kind="fact", confidence=0.8)
        correction = fact(predicate="居住城市", object="北京", kind="fact", confidence=0.95)
        with self.brain(extraction_with_fact(old), extraction_with_fact(correction)) as brain:
            brain.consolidate("用户：我住在上海。")
            old_id = brain.store.list_semantic()[0].id
            report = brain.consolidate("用户：纠正一下，我现在住北京。")
            old_memory = brain.store.get_semantic(old_id)
            new_memory = next(item for item in brain.store.list_semantic() if item.id != old_id)
            self.assertTrue(old_memory.conflict_flag)
            self.assertAlmostEqual(old_memory.confidence, 0.4)
            self.assertFalse(new_memory.conflict_flag)
            self.assertIn(old_id, report.conflicts)

    def test_conflict_prunes_stale_core_profile_string_but_keeps_semantic_memory(self) -> None:
        old = fact(predicate="依赖", object="喝白茶来放松", importance=0.6)
        correction = fact(predicate="偏好饮用", object="绿茶", importance=0.35)
        first = ExtractionResult(
            semantic_facts=[old],
            core_candidate_updates=CoreCandidateUpdates(
                user_profile_changes=[
                    ProfileChange("core_preferences", "最近特别依赖喝白茶来放松", "add")
                ]
            ),
        )
        with self.brain(first) as brain:
            brain.consolidate("用户：我依赖喝白茶来放松。")
            old_id = brain.store.list_semantic()[0].id
            self.assertIn(
                "最近特别依赖喝白茶来放松",
                brain.core.user_stable_profile.core_preferences,
            )
            second = ExtractionResult(
                semantic_facts=[correction],
                conflicts_detected=[ConflictDetection(old_id, "用户已改喝绿茶")],
            )
            brain.extractor.results.append(second)

            report = brain.consolidate("用户：我现在改喝绿茶了。")

            old_memory = brain.store.get_semantic(old_id)
            self.assertIsNotNone(old_memory)  # 语义记忆本身不被删除
            self.assertTrue(old_memory.conflict_flag)
            self.assertAlmostEqual(old_memory.confidence, 0.45)
            self.assertEqual(len(brain.store.list_semantic()), 2)

            self.assertIn("最近特别依赖喝白茶来放松", report.core_pruned)
            self.assertNotIn(
                "最近特别依赖喝白茶来放松",
                brain.core.user_stable_profile.core_preferences,
            )

            core_audit = next(
                item
                for item in brain.store.list_audit()
                if item["entity_type"] == "core" and item["operation"].startswith("consolidate")
            )
            self.assertIn(
                "最近特别依赖喝白茶来放松",
                core_audit["before"]["user_stable_profile"]["core_preferences"],
            )
            self.assertNotIn(
                "最近特别依赖喝白茶来放松",
                core_audit["after"]["user_stable_profile"]["core_preferences"],
            )

    def test_conflict_prune_handles_paraphrased_core_text(self) -> None:
        old = fact(predicate="依赖", object="喝白茶放松", importance=0.6)
        correction = fact(predicate="偏好饮用", object="绿茶", importance=0.35)
        first = ExtractionResult(
            semantic_facts=[old],
            core_candidate_updates=CoreCandidateUpdates(
                user_profile_changes=[
                    ProfileChange("core_preferences", "依赖喝白茶来放松", "add")
                ]
            ),
        )
        with self.brain(first) as brain:
            brain.consolidate("用户：我依赖喝白茶放松。")
            old_id = brain.store.list_semantic()[0].id
            second = ExtractionResult(
                semantic_facts=[correction],
                core_candidate_updates=CoreCandidateUpdates(
                    user_profile_changes=[
                        ProfileChange("core_preferences", "现在改喝绿茶，觉得白茶太淡", "add")
                    ]
                ),
                conflicts_detected=[ConflictDetection(old_id, "用户改喝绿茶")],
            )
            brain.extractor.results.append(second)

            report = brain.consolidate("用户：我现在改喝绿茶，白茶太淡。")

            self.assertIn("依赖喝白茶来放松", report.core_pruned)
            prefs = brain.core.user_stable_profile.core_preferences
            self.assertNotIn("依赖喝白茶来放松", prefs)
            self.assertIn("现在改喝绿茶，觉得白茶太淡", prefs)

    def test_core_limit_failure_rolls_back_entire_consolidation(self) -> None:
        result = extraction_with_fact()
        result.core_candidate_updates.user_profile_changes.append(
            ProfileChange("important_life_facts", "重大经历" * 1000, "add")
        )
        with self.brain(result) as brain:
            with self.assertRaises(CoreMemoryLimitError):
                brain.consolidate("用户：我喜欢白茶。")
            self.assertEqual(brain.store.list_semantic(), [])
            self.assertEqual(brain.store.list_episodic(), [])

    def test_decay_archives_stale_low_value_memory_but_protects_core_memory(self) -> None:
        old_time = NOW - timedelta(days=1000)
        with self.brain(ExtractionResult()) as brain:
            brain.store.save_semantic(
                semantic("sem_stale", timestamp=old_time, importance=0.3,
                         valence="neutral", intensity=0.0)
            )
            brain.store.save_semantic(
                semantic("sem_protected", timestamp=old_time, importance=0.3, protected=True)
            )
            brain.core.protected_memories.append("sem_protected")
            brain.save_core(brain.core)

            brain._consolidator.consolidate("用户：没有需要记录的内容。", now=NOW)
            self.assertTrue(brain.store.get_semantic("sem_stale").archived)
            self.assertLess(brain.store.get_semantic("sem_stale").decay_score, 0.05)
            protected = brain.store.get_semantic("sem_protected")
            self.assertFalse(protected.archived)
            self.assertEqual(protected.decay_score, 1.0)

    def test_relationship_memory_is_promoted_after_repeated_activation(self) -> None:
        memory = semantic("sem_relationship")
        memory.type = "relationship"
        memory.importance = 0.95
        memory.activation_count = 3
        with self.brain(ExtractionResult()) as brain:
            brain.store.save_semantic(memory)
            report = brain._consolidator.consolidate("用户：今天不用整理新记忆。", now=NOW)
            self.assertIn("sem_relationship", report.promoted)
            self.assertIn("sem_relationship", brain.core.protected_memories)
            self.assertTrue(brain.store.get_semantic("sem_relationship").protected)

    def test_decay_formula_matches_specification(self) -> None:
        timestamp = to_iso8601(NOW - timedelta(days=10))
        score = calculate_decay(
            importance=0.8,
            last_activated=timestamp,
            activation_count=0,
            protected=False,
            now=NOW,
            decay_lambda=0.06,
        )
        self.assertAlmostEqual(score, 0.8 * pow(2.718281828459045, -0.6), places=6)
        protected = calculate_decay(
            importance=0.1,
            last_activated=timestamp,
            activation_count=0,
            protected=True,
            now=NOW,
            decay_lambda=0.06,
        )
        self.assertEqual(protected, 1.0)


class SessionTrackerTests(BrainTestCase):
    def test_auto_consolidation_threshold_and_reset(self) -> None:
        with self.brain(extraction_with_fact()) as brain:
            tracker = SessionTracker(brain, consolidate_every=3)
            self.assertEqual(tracker.start_session("s-test"), "s-test")
            self.assertIsNone(tracker.add("user", "我喜欢白茶。", auto_consolidate=False))
            self.assertIsNone(tracker.add("assistant", "记下了。", auto_consolidate=False))
            self.assertEqual(tracker.pending_count, 2)
            report = tracker.add("user", "还有别的吗？")  # 第 3 条触发自动巩固
            self.assertIsNotNone(report)
            self.assertEqual(report.semantic_written, 1)
            self.assertEqual(tracker.pending_count, 0)
            self.assertIsNone(tracker.consolidate())  # 空缓冲区返回 None

    def test_inject_uses_stored_memory(self) -> None:
        with self.brain() as brain:
            brain.store.save_semantic(semantic())
            brain.reindex()
            tracker = SessionTracker(brain)
            tracker.start_session("s")
            injection = tracker.inject("白茶")
            self.assertIn("白茶", injection.context)
            self.assertIn("sem_20240101_test", injection.retrieved.memory_ids)


class RetrievalTests(BrainTestCase):
    def test_scores_entity_filter_and_injection_activation(self) -> None:
        with self.brain() as brain:
            brain.store.save_semantic(semantic())
            unrelated = semantic("sem_unrelated")
            unrelated.subject = "项目"
            unrelated.predicate = "使用"
            unrelated.object = "数据库索引"
            unrelated.detail = "一次性技术实现细节。"
            brain.store.save_semantic(unrelated)
            brain.reindex()

            result = brain.retrieve("用户喜欢喝什么白茶？")
            self.assertEqual(result.semantic[0].memory.id, "sem_20240101_test")
            score = result.semantic[0]
            expected = (
                0.40 * score.semantic_similarity
                + 0.25 * score.importance
                + 0.20 * score.recency_boost
                + 0.15 * score.emotion_match
            )
            self.assertAlmostEqual(score.final_score, expected)

            filtered = brain.retrieve("数据库怎么建索引？", entities=["数据库"])
            self.assertTrue(all("数据库" in item.memory.embedding_text() for item in filtered.semantic))

            injection = brain.build_injection("我想喝白茶，有点难过。")
            self.assertIn("Core，始终完整保留", injection.context)
            self.assertIn("sem_20240101_test", injection.context)
            self.assertLessEqual(injection.estimated_tokens, injection.token_limit)
            self.assertEqual(brain.store.get_semantic("sem_20240101_test").activation_count, 1)

    def test_injection_budget_drops_candidates_but_never_truncates_core(self) -> None:
        with self.brain() as brain:
            brain.store.save_semantic(semantic())
            brain.reindex()
            empty = brain.retriever.build_injection(brain.core, RetrievalResult())
            limited = brain.build_injection("白茶", token_limit=empty.estimated_tokens)
            self.assertEqual(limited.retrieved.memory_ids, [])
            self.assertNotIn("相关语义记忆", limited.context)
            self.assertIn(brain.core.identity.name, limited.context)
            self.assertEqual(brain.store.get_semantic("sem_20240101_test").activation_count, 0)

    def test_new_session_can_recall_emotional_episode(self) -> None:
        result = ExtractionResult(
            episodic_events=[
                EpisodicCandidate(
                    summary="用户在离职那天感到孤独，陪伴让用户安心。",
                    key_moments=["那天你没有催我振作，只是陪着我。"],
                    emotions=["孤独", "安心", "感动"],
                    importance=0.95,
                )
            ]
        )
        with self.brain(result) as brain:
            brain.consolidate("用户：那天你没有催我振作，只是陪着我。")
        with ExtraBrain(self.db_path) as new_session:
            injection = new_session.build_injection("我又想起离职那天，有点孤独。")
            self.assertIn("那天你没有催我振作", injection.context)


if __name__ == "__main__":
    unittest.main()
