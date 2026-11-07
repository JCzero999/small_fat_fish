"""v0.13.3 Chat Shell 记忆交互改版测试。

覆盖：自动巩固策略下发（默认值 / user_config / 环境变量）、静态 UI 主路径干净、
高级面板保留调试能力、按「未巩固切片」巩固的 API 契约、只读模式、默认不每轮巩固。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extra_brain.extraction import ExtractionResult, SemanticCandidate  # noqa: E402
from extra_brain.server import ExtraBrainServer, ShellSettings, make_server  # noqa: E402

SHELL_TOKEN = "test-token-123"


def _fact(object_: str) -> ExtractionResult:
    return ExtractionResult(
        semantic_facts=[
            SemanticCandidate(
                type="preference",
                subject="用户",
                predicate="喜欢",
                object=object_,
                detail=f"用户喜欢{object_}。",
                importance=0.7,
                emotion_valence="positive",
                emotion_intensity=0.5,
                confidence=0.9,
            )
        ]
    )


class CountingExtractor:
    """按顺序返回结果，并记录抽取调用次数（用于验证「不每轮巩固」）。"""

    def __init__(self, *results: ExtractionResult) -> None:
        self.results = list(results)
        self.calls = 0

    def extract(self, prompt: str) -> ExtractionResult:
        self.calls += 1
        if not self.results:
            return ExtractionResult()
        return self.results.pop(0)


class FakeChatClient:
    def __init__(self, reply: str = "嗯，我在。") -> None:
        self.reply = reply
        self.calls = 0

    def complete(self, messages, *, model=None, temperature=0.8, max_tokens=None):
        self.calls += 1
        return {"content": self.reply, "usage": {"total_tokens": 10}}


def _shell(token: str = SHELL_TOKEN, **kwargs) -> ShellSettings:
    return ShellSettings(enabled=True, peer_id="local-main", token=token, env={}, **kwargs)


class ShellPolicyTests(unittest.TestCase):
    """策略解析：默认值 / user_config / 环境变量 / 非法值兜底。"""

    def test_defaults(self) -> None:
        shell = ShellSettings(enabled=True, token="t", env={}, user_config={})
        self.assertEqual(
            shell.auto_consolidate_policy(),
            {
                "enabled": True,
                "every_n_user_messages": 8,
                "on_new_session": True,
                "on_page_hide": True,
                "min_messages": 2,
            },
        )

    def test_user_config_override(self) -> None:
        shell = ShellSettings(
            enabled=True,
            token="t",
            env={},
            user_config={
                "shell": {
                    "auto_consolidate_enabled": False,
                    "auto_consolidate_every_n_user_messages": 3,
                    "auto_consolidate_on_new_session": False,
                    "auto_consolidate_on_page_hide": False,
                    "auto_consolidate_min_messages": 1,
                }
            },
        )
        policy = shell.auto_consolidate_policy()
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["every_n_user_messages"], 3)
        self.assertFalse(policy["on_new_session"])
        self.assertFalse(policy["on_page_hide"])
        self.assertEqual(policy["min_messages"], 1)

    def test_env_override(self) -> None:
        shell = ShellSettings(
            enabled=True,
            token="t",
            env={
                "SHELL_AUTO_CONSOLIDATE_ENABLED": "off",
                "SHELL_AUTO_CONSOLIDATE_EVERY_N_USER_MESSAGES": "5",
                "SHELL_AUTO_CONSOLIDATE_ON_NEW_SESSION": "0",
                "SHELL_AUTO_CONSOLIDATE_MIN_MESSAGES": "4",
            },
            user_config={},
        )
        policy = shell.auto_consolidate_policy()
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["every_n_user_messages"], 5)
        self.assertFalse(policy["on_new_session"])
        self.assertEqual(policy["min_messages"], 4)

    def test_invalid_values_fall_back(self) -> None:
        shell = ShellSettings(
            enabled=True,
            token="t",
            env={},
            user_config={"shell": {"auto_consolidate_every_n_user_messages": "abc"}},
        )
        policy = shell.auto_consolidate_policy()
        self.assertEqual(policy["every_n_user_messages"], 8)
        self.assertGreaterEqual(policy["min_messages"], 1)

    def test_explicit_args_win(self) -> None:
        shell = ShellSettings(
            enabled=True,
            token="t",
            env={"SHELL_AUTO_CONSOLIDATE_EVERY_N_USER_MESSAGES": "5"},
            user_config={"shell": {"auto_consolidate_every_n_user_messages": 3}},
            auto_consolidate_every_n_user_messages=6,
        )
        self.assertEqual(shell.auto_consolidate_policy()["every_n_user_messages"], 6)


class ShellServerTestCase(unittest.TestCase):
    def start(self, *, extractor=None, chat_client=None, shell=None, user_config=None,
              read_only: bool = False) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.server = make_server(
            self.tempdir.name,
            extractor=extractor,
            host="127.0.0.1",
            port=0,
            shell=shell or _shell(),
            user_config=user_config if user_config is not None else {},
            chat_client=chat_client,
        )
        if read_only:
            self.server.app.read_only = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.app.close()
        self.tempdir.cleanup()

    def request(self, method, path, body=None, token: str | None = SHELL_TOKEN):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read()
                if "application/json" in ctype:
                    return resp.status, json.loads(raw.decode("utf-8"))
                return resp.status, raw.decode("utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8")
            exc.close()
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, payload


class HealthPolicyEndpointTests(ShellServerTestCase):
    def test_health_carries_policy(self) -> None:
        self.start(extractor=CountingExtractor(_fact("白茶")), chat_client=FakeChatClient())
        try:
            status, body = self.request("GET", "/v1/health", token=None)
            self.assertEqual(status, 200)
            self.assertIn("auto_consolidate", body)
            policy = body["auto_consolidate"]
            self.assertTrue(policy["enabled"])
            self.assertEqual(policy["every_n_user_messages"], 8)
            self.assertEqual(policy["min_messages"], 2)
        finally:
            self.stop()

    def test_health_policy_from_user_config(self) -> None:
        cfg = {"shell": {"auto_consolidate_every_n_user_messages": 4, "auto_consolidate_min_messages": 3}}
        self.start(
            extractor=CountingExtractor(_fact("白茶")),
            chat_client=FakeChatClient(),
            shell=ShellSettings(enabled=True, peer_id="local-main", token=SHELL_TOKEN, env={}, user_config=cfg),
            user_config=cfg,
        )
        try:
            _, body = self.request("GET", "/v1/health", token=None)
            self.assertEqual(body["auto_consolidate"]["every_n_user_messages"], 4)
            self.assertEqual(body["auto_consolidate"]["min_messages"], 3)
        finally:
            self.stop()

    def test_read_only_health_reports_not_writable(self) -> None:
        self.start(extractor=None, chat_client=FakeChatClient())
        try:
            _, body = self.request("GET", "/v1/health", token=None)
            self.assertFalse(body["read_write"])
        finally:
            self.stop()


class StaticShellUiTests(ShellServerTestCase):
    def setUp(self) -> None:
        self.start(extractor=CountingExtractor(_fact("白茶")), chat_client=FakeChatClient())

    def tearDown(self) -> None:
        self.stop()

    def _html(self) -> str:
        status, body = self.request("GET", "/shell/", token=None)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, str)
        return body

    def test_main_path_has_no_memory_buttons(self) -> None:
        html = self._html()
        # 主路径不再出现「记忆管理」按钮
        self.assertNotIn("记住以上对话", html)
        self.assertNotIn("btn-remember", html)
        # 发送 + 新会话仍在
        self.assertIn('id="btn-send"', html)
        self.assertIn('id="btn-new"', html)

    def test_advanced_panel_keeps_debug_capabilities(self) -> None:
        html = self._html()
        self.assertIn('id="advanced"', html)
        self.assertIn("高级", html)
        self.assertIn("立即写入记忆", html)
        self.assertIn("预览本轮注入", html)
        self.assertIn("/v1/inject_preview", html)
        self.assertIn("/v1/consolidate", html)

    def test_auto_consolidate_wiring_present(self) -> None:
        html = self._html()
        self.assertIn("auto_consolidate", html)          # 从 /v1/health 读策略
        self.assertIn("visibilitychange", html)          # 页面隐藏冲刷
        self.assertIn("pagehide", html)
        self.assertIn("keepalive", html)                 # fetch keepalive
        self.assertIn("consolidatedUpTo", html)          # 只送未巩固切片

    def test_token_entry_still_available(self) -> None:
        html = self._html()
        self.assertIn('id="token-badge"', html)
        self.assertIn('id="token-mask"', html)


class SliceConsolidateTests(ShellServerTestCase):
    """前端按「未巩固切片」调用 consolidate——每片独立可用，不重复全量。"""

    def setUp(self) -> None:
        self.start(extractor=CountingExtractor(_fact("白茶"), _fact("普洱")), chat_client=FakeChatClient())

    def tearDown(self) -> None:
        self.stop()

    def test_two_slices_consolidate_independently(self) -> None:
        first = [
            {"role": "user", "content": "我喜欢白茶。"},
            {"role": "assistant", "content": "记住啦。"},
        ]
        status, body = self.request(
            "POST", "/v1/consolidate",
            {"peer_id": "local-main", "session_id": "s1", "messages": first},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["semantic_written"], 1)

        second = [
            {"role": "user", "content": "也喜欢普洱。"},
            {"role": "assistant", "content": "好。"},
        ]
        status, body = self.request(
            "POST", "/v1/consolidate",
            {"peer_id": "local-main", "session_id": "s1", "messages": second},
        )
        self.assertEqual(status, 200)
        # 第二片独立巩固（数量取决于冲突/版本链处理，只要确实写入了即可）
        self.assertGreaterEqual(body["semantic_written"], 1)

    def test_consolidate_rejects_empty_messages(self) -> None:
        status, _ = self.request(
            "POST", "/v1/consolidate", {"peer_id": "local-main", "messages": []}
        )
        self.assertEqual(status, 400)


class ReadOnlyConsolidateTests(ShellServerTestCase):
    def test_consolidate_503_without_extractor(self) -> None:
        self.start(extractor=None, chat_client=FakeChatClient())
        try:
            status, body = self.request(
                "POST", "/v1/consolidate",
                {"peer_id": "local-main", "messages": [{"role": "user", "content": "hi"}]},
            )
            self.assertEqual(status, 503)
            self.assertIn("read-only", body["error"])
        finally:
            self.stop()


class NoPerTurnConsolidateTests(ShellServerTestCase):
    def test_chat_does_not_consolidate_by_default(self) -> None:
        extractor = CountingExtractor(_fact("白茶"))
        self.start(extractor=extractor, chat_client=FakeChatClient())
        try:
            for i in range(3):
                status, body = self.request(
                    "POST", "/v1/chat", {"message": f"第{i}句", "peer_id": "local-main"}
                )
                self.assertEqual(status, 200)
                self.assertNotIn("auto_consolidate", body)
            self.assertEqual(extractor.calls, 0)  # 默认每轮都不巩固
        finally:
            self.stop()


if __name__ == "__main__":
    unittest.main()
