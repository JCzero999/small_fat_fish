"""v0.12.1 表达与人设层测试：T4 honesty_footer + T3 群/私聊 peer 隔离回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from extra_brain import ExtraBrain
from extra_brain.engine import _HONESTY_LINE
from extra_brain.storage import WriteBatch
from extra_brain.models import SemanticMemory, to_iso8601
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=TZ)


def sem(memory_id: str, **ov):
    base = dict(
        id=memory_id, type="fact", subject="用户", predicate="拥有", object="秘密",
        detail="只有私聊知道的事。", importance=0.8, emotion_valence="neutral",
        emotion_intensity=0.0, confidence=0.9, source_episodic_ids=[],
        created_at=to_iso8601(NOW), last_activated=to_iso8601(NOW),
    )
    base.update(ov)
    return SemanticMemory(**base)


class HonestyFooterTests(unittest.TestCase):
    """T4：诚实契约行开关（默认关=输出零变化；开=首行注入）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "h121.db"

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def _inject(self, honesty: bool) -> str:
        brain = ExtraBrain(self.db, honesty_footer=honesty)
        self.addCleanup(brain.close)
        return brain.build_injection("你好呀").context

    def test_default_off_no_contract_line(self):
        self.assertNotIn("【契约】", self._inject(False))

    def test_on_prepends_contract_line(self):
        context = self._inject(True)
        self.assertTrue(context.startswith(_HONESTY_LINE))

    def test_off_after_on_still_clean(self):
        self._inject(True)
        self.assertNotIn("【契约】", self._inject(False))


class GroupPeerIsolationTests(unittest.TestCase):
    """T3 回归：私聊与群聊各按稳定 peer（QQ号/群号）分库，记忆互不可见。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def _brain(self, tag: str) -> ExtraBrain:
        brain = ExtraBrain(self.base / f"peer_{tag}.db")
        self.addCleanup(brain.close)
        return brain

    def test_private_memory_invisible_to_group(self):
        private = self._brain("FriendMessage_10001")
        batch = WriteBatch(operation="test.seed_private")
        batch.semantic.append(sem("sem_secret"))
        private.store.apply_batch(batch)
        private.reindex()

        group = self._brain("GroupMessage_55555")
        group.reindex()
        context = group.build_injection("用户拥有什么秘密").context
        self.assertNotIn("sem_secret", context)
        self.assertEqual(group.store.get_memory("sem_secret"), None)

    def test_peer_ids_stable_shape(self):
        # 适配器 resolve_peer_id 的群/私聊形态回归（与 HANDOVER §6 坑 1 一致）
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                              / "astrbot-plugin-extra-brain"))
        try:
            from main import resolve_peer_id  # type: ignore
        except ImportError:
            self.skipTest("插件 main.py 需要 astrbot 依赖，CI 环境跳过")
        self.assertEqual(
            resolve_peer_id("aiocqhttp:GroupMessage:55555", "auto"),
            "aiocqhttp:GroupMessage:55555",
        )
        self.assertEqual(
            resolve_peer_id("aiocqhttp:FriendMessage:10001", "auto"),
            "aiocqhttp:FriendMessage:10001",
        )


if __name__ == "__main__":
    unittest.main()
