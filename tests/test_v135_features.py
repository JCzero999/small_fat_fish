"""v0.13.5：系统常驻（避免息屏后新型待机冻结进程）回归测试。

背景：2026-11-09 实测两次进程整体冻结（16:17 Idle Timeout、18:18 合盖，各约 56 分钟），
期间 QQ 消息完全收不到，表现为「发两三次才回」。修复是让 Shell 进程持有
SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) 电源请求。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extra_brain import server as server_mod
from extra_brain.server import (
    ExtraBrainServer,
    ShellSettings,
    _install_keep_awake,
)

REPO = Path(__file__).resolve().parent.parent


class KeepAwakeFlagTests(unittest.TestCase):
    def test_env_switch_can_disable(self) -> None:
        with patch.dict(os.environ, {"SHELL_KEEP_AWAKE": "0"}):
            self.assertFalse(_install_keep_awake())

    def test_env_switch_accepts_off_spellings(self) -> None:
        for value in ("0", "off", "false", "no", "OFF"):
            with patch.dict(os.environ, {"SHELL_KEEP_AWAKE": value}):
                self.assertFalse(_install_keep_awake(), value)

    def test_returns_true_on_windows_by_default(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows only")
        with patch.dict(os.environ, {"SHELL_KEEP_AWAKE": "1"}):
            self.assertTrue(_install_keep_awake())


class HealthPayloadTests(unittest.TestCase):
    def test_health_exposes_keep_awake(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = ExtraBrainServer(
                tmp,
                shell=ShellSettings(enabled=True, peer_id="local-main", token="t", env={}),
                user_config={},
            )
            health = app.shell_health()
            self.assertIn("keep_awake", health)
            self.assertIsInstance(health["keep_awake"], bool)
            self.assertTrue(health["ok"])

    def test_health_keep_awake_reflects_module_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = ExtraBrainServer(
                tmp,
                shell=ShellSettings(enabled=True, peer_id="local-main", token="t", env={}),
                user_config={},
            )
            with patch.object(server_mod, "_KEEP_AWAKE_ACTIVE", True):
                self.assertTrue(app.shell_health()["keep_awake"])
            with patch.object(server_mod, "_KEEP_AWAKE_ACTIVE", False):
                self.assertFalse(app.shell_health()["keep_awake"])


class ScriptsTests(unittest.TestCase):
    def test_keep_awake_script_present(self) -> None:
        ps1 = REPO / "scripts" / "keep-awake.ps1"
        self.assertTrue(ps1.exists())
        text = ps1.read_text(encoding="utf-8-sig")
        self.assertIn("SetThreadExecutionState", text)
        self.assertIn("ES_SYSTEM_REQUIRED", text)
        # 允许息屏、只锁系统：不能顺手把屏幕也锁死
        self.assertNotIn("ES_DISPLAY_REQUIRED", text)

    def test_keep_awake_ps1_has_bom(self) -> None:
        """Windows PowerShell 5.1 按 ANSI 读无 BOM 的 UTF-8：中文注释会吃掉后面的 ASCII，导致语法错误。"""
        raw = (REPO / "scripts" / "keep-awake.ps1").read_bytes()
        self.assertEqual(raw[:3], b"\xef\xbb\xbf")

    def test_restart_all_wires_keep_awake(self) -> None:
        text = (REPO / "scripts" / "restart-all.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Start-KeepAwake", text)
        self.assertIn("NoKeepAwake", text)


if __name__ == "__main__":
    unittest.main()
