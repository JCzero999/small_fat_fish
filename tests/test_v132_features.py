"""v0.13.2 主人身份卡（owner_accounts）：跨 peer/跨宿主识别用户。

- UserStableProfile 新字段 owner_accounts 的校验/序列化/向后兼容
- engine.set_owner_card 写全局伴侣层，其他 peer 经 _merged_core 可见
- 服务端 /v1/set_owner（鉴权）与 /v1/owner
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from extra_brain.engine import ExtraBrain
from extra_brain.models import CoreMemory, MemoryValidationError, UserStableProfile
from extra_brain.server import ExtraBrainServer, ShellSettings

OWNER_CARD = "主人QQ:1975824324|称呼:JCzero|关系:父女"


class TestOwnerProfileModel(unittest.TestCase):
    def test_owner_accounts_roundtrip(self) -> None:
        p = UserStableProfile.from_dict({"owner_accounts": [OWNER_CARD]})
        self.assertEqual(p.owner_accounts, [OWNER_CARD])
        d = p.to_dict()
        self.assertEqual(d["owner_accounts"], [OWNER_CARD])

    def test_owner_accounts_backward_compatible(self) -> None:
        """旧库 Core JSON 无该键 → 默认空列表，不报错。"""
        core = CoreMemory.from_dict({"identity": {"name": "小豚"}})
        self.assertEqual(core.user_stable_profile.owner_accounts, [])

    def test_owner_accounts_must_be_strings(self) -> None:
        with self.assertRaises(MemoryValidationError):
            UserStableProfile(owner_accounts=[123])


class TestOwnerCardEngine(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.global_db = root / "_global_companion.db"
        # 私聊 peer + 群聊 peer，都挂同一个全局伴侣库
        self.private = ExtraBrain(root / "private.db", companion_db=self.global_db)
        self.group = ExtraBrain(root / "group.db", companion_db=self.global_db)

    def tearDown(self) -> None:
        self.private.close()
        self.group.close()
        self._tmp.cleanup()

    def test_set_owner_writes_to_companion(self) -> None:
        report = self.private.set_owner_card([OWNER_CARD])
        self.assertTrue(report["written"])
        self.assertEqual(report["written_to"], "companion")
        self.assertEqual(self.private.companion.core.user_stable_profile.owner_accounts, [OWNER_CARD])

    def test_owner_card_visible_from_group_peer(self) -> None:
        """群聊 peer 的合并 Core 里能看到主人身份卡（私聊写入，群聊生效）。"""
        self.private.set_owner_card([OWNER_CARD])
        merged = self.group._merged_core()
        self.assertIn(OWNER_CARD, merged.user_stable_profile.owner_accounts)

    def test_set_owner_idempotent(self) -> None:
        self.private.set_owner_card([OWNER_CARD])
        again = self.private.set_owner_card([OWNER_CARD])
        self.assertFalse(again["written"])
        # 重复条目不堆积
        self.assertEqual(len(self.private.get_owner_card()), 1)

    def test_set_owner_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            self.private.set_owner_card(["", "  "])


class TestOwnerServerEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = ExtraBrainServer(
            Path(self._tmp.name),
            extractor=None,
            shell=ShellSettings(enabled=True, peer_id="qq:FriendMessage:10000", token="tok", env={}),
        )

    def tearDown(self) -> None:
        self.app.close()
        self._tmp.cleanup()

    def _handle(self, method, path, body=None, token="tok"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.app.handle(method, path, body or {}, headers=headers)

    def test_set_and_get_owner(self) -> None:
        r = self._handle("POST", "/v1/set_owner", {"entries": [OWNER_CARD]})
        self.assertTrue(r["written"])
        g = self._handle("GET", "/v1/owner")
        self.assertEqual(g["owner_accounts"], [OWNER_CARD])

    def test_set_owner_requires_auth(self) -> None:
        from extra_brain.server import _HttpError
        with self.assertRaises(_HttpError) as ctx:
            self._handle("POST", "/v1/set_owner", {"entries": [OWNER_CARD]}, token=None)
        self.assertEqual(ctx.exception.status.value, 401)

    def test_set_owner_rejects_empty_entries(self) -> None:
        from extra_brain.server import _HttpError
        with self.assertRaises(_HttpError) as ctx:
            self._handle("POST", "/v1/set_owner", {"entries": []})
        self.assertEqual(ctx.exception.status.value, 400)


if __name__ == "__main__":
    unittest.main()
