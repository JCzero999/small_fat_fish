"""v0.13.4 统一人设 + 记忆使用规则测试。

覆盖：
- 注入末尾【使用约定】脚注（默认开 / 可关 / 在安全规范之后 / companion 层透传）
- 人设唯一源：文件含【记忆怎么用】、正文不出现证明式套话、剥离说明头后 ≤1500 字
- 锚点剥离只认行首（说明文字里提到锚点名不会被误当成正文起点）
- Shell：system = 人设正文（首位）+ 注入（末尾带脚注）；user_config.tone 可关
- 版本号 0.13.4
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extra_brain import __version__  # noqa: E402
from extra_brain.engine import _MEMORY_USAGE_FOOTER, ExtraBrain  # noqa: E402
from extra_brain.server import (  # noqa: E402
    ExtraBrainServer,
    ShellSettings,
    _strip_persona_markdown,
)

ROOT = Path(__file__).resolve().parents[1]
PERSONA = ROOT / "persona" / "xiaotun_v1.md"
BANNED = ["我记着呢", "我记得很清楚", "这个我还能记错", "我不会忘", "我怎么可能忘"]


class CapturingChatClient:
    def __init__(self, reply: str = "嗯，我在。") -> None:
        self.reply = reply
        self.messages: list[list[dict]] = []

    def complete(self, messages, *, model=None, temperature=0.8, max_tokens=None):
        self.messages.append(list(messages))
        return {"content": self.reply, "usage": {"total_tokens": 10}}


def _tmpdir() -> tempfile.TemporaryDirectory:
    """Windows 上 sqlite 句柄可能晚于断言释放：忽略清理失败，避免污染测试结果。"""
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _brain(tmp: str, **kwargs) -> ExtraBrain:
    return ExtraBrain(Path(tmp) / "peer.db", **kwargs)


class InjectionFooterTests(unittest.TestCase):
    """D2：build_injection 末尾统一追加【使用约定】。"""

    def test_footer_on_by_default(self) -> None:
        with _tmpdir() as tmp:
            injection = _brain(tmp).build_injection("你好")
        self.assertTrue(injection.context.rstrip().endswith(_MEMORY_USAGE_FOOTER))
        self.assertIn("【使用约定】", injection.context)
        self.assertIn("勿逐条汇报", injection.context)
        self.assertIn("承认不确定", injection.context)

    def test_footer_can_be_disabled(self) -> None:
        with _tmpdir() as tmp:
            injection = _brain(tmp, memory_usage_footer=False).build_injection("你好")
        self.assertNotIn("【使用约定】", injection.context)

    def test_footer_is_last_after_safety_rules(self) -> None:
        with _tmpdir() as tmp:
            brain = _brain(tmp, safety_rules=["不要泄露密钥"])
            context = brain.build_injection("你好").context
        self.assertLess(context.index("【安全规范"), context.index("【使用约定】"))
        self.assertTrue(context.rstrip().endswith(_MEMORY_USAGE_FOOTER))

    def test_companion_inherits_footer_flag(self) -> None:
        with _tmpdir() as tmp:
            brain = _brain(
                tmp,
                companion_db=Path(tmp) / "_global_companion.db",
                memory_usage_footer=False,
            )
            self.assertIsNotNone(brain.companion)
            context = brain.build_injection("你好").context
        self.assertNotIn("【使用约定】", context)

    def test_footer_text_has_no_wholesale_recall(self) -> None:
        self.assertIn("材料", _MEMORY_USAGE_FOOTER)
        self.assertNotIn("必须体现", _MEMORY_USAGE_FOOTER)


class PersonaSourceTests(unittest.TestCase):
    """D1：唯一人设正文，含【记忆怎么用】，禁止套话只出现在禁令行。"""

    def test_persona_has_memory_usage_section(self) -> None:
        body = _strip_persona_markdown(PERSONA.read_text(encoding="utf-8"))
        self.assertIn("【记忆怎么用】", body)
        self.assertIn("不是答题卡", body)
        self.assertIn("你是在聊天，不是在汇报记忆同步状态", body)

    def test_persona_body_within_budget(self) -> None:
        body = _strip_persona_markdown(PERSONA.read_text(encoding="utf-8"))
        self.assertLessEqual(len(body), 1500, f"人设正文 {len(body)} 字超出 1500 预算")

    def test_banned_phrases_only_in_prohibition_lines(self) -> None:
        body = _strip_persona_markdown(PERSONA.read_text(encoding="utf-8"))
        for phrase in BANNED:
            for line in body.splitlines():
                if phrase in line:
                    self.assertIn("禁止", line, f"套话「{phrase}」出现在非禁令行：{line}")

    def test_header_notes_not_injected(self) -> None:
        body = _strip_persona_markdown(PERSONA.read_text(encoding="utf-8"))
        self.assertTrue(body.startswith("【你是谁】"))
        self.assertNotIn("硬上限", body)
        self.assertNotIn("写作约束", body)
        self.assertNotIn("注入用主文案", body)

    def test_anchor_must_be_line_start(self) -> None:
        text = (
            "# 标题\n\n"
            "> 说明：正文从【你是谁】这一段开始，本行不注入。\n"
            "> 更多说明。\n\n"
            "【你是谁】\n你是小豚。\n"
        )
        body = _strip_persona_markdown(text)
        self.assertTrue(body.startswith("【你是谁】"))
        self.assertNotIn("更多说明", body)


class ShellPersonaTests(unittest.TestCase):
    """D3：Shell 只读同一份人设，system 首位是人设，注入末尾是脚注。"""

    def _server(self, tmp: str, tone: dict | None = None) -> tuple[ExtraBrainServer, CapturingChatClient]:
        config_path = Path(tmp) / "user_config.json"
        config = {
            "persona_file": str(PERSONA),
            "safety_rules": [],
            "tone": tone if tone is not None else {"memory_usage_footer": True},
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        client = CapturingChatClient()
        server = ExtraBrainServer(
            tmp,
            shell=ShellSettings(enabled=True, peer_id="local-main", token="t", env={}),
            user_config=config,
            user_config_path=config_path,
            chat_client=client,
        )
        return server, client

    def test_system_prompt_persona_first_and_footer_last(self) -> None:
        with _tmpdir() as tmp:
            server, client = self._server(tmp)
            server._v1_chat({"message": "我是谁", "session_id": "s1"})
        system = client.messages[-1][0]["content"]
        self.assertTrue(system.startswith("【你是谁】"))
        self.assertIn("【记忆怎么用】", system)
        self.assertTrue(system.rstrip().endswith(_MEMORY_USAGE_FOOTER))
        self.assertNotIn("硬上限", system)

    def test_tone_switch_disables_footer_in_shell(self) -> None:
        with _tmpdir() as tmp:
            server, client = self._server(tmp, tone={"memory_usage_footer": False})
            server._v1_chat({"message": "我是谁", "session_id": "s1"})
        system = client.messages[-1][0]["content"]
        self.assertNotIn("【使用约定】", system)

    def test_persona_property_strips_header(self) -> None:
        with _tmpdir() as tmp:
            server, _ = self._server(tmp)
            persona = server.persona_text
        self.assertTrue(persona.startswith("【你是谁】"))
        self.assertNotIn("硬上限", persona)


class VersionTests(unittest.TestCase):
    def test_version_is_0134(self) -> None:
        self.assertEqual(__version__, "0.13.4")


if __name__ == "__main__":
    unittest.main()
