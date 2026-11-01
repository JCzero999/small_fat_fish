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

from extra_brain.extraction import ExtractionResult  # noqa: E402
from extra_brain.extraction import SemanticCandidate  # noqa: E402
from extra_brain.server import ExtraBrainServer, make_server  # noqa: E402


def _fact(object_: str = "白茶") -> ExtractionResult:
    return ExtractionResult(
        semantic_facts=[
            SemanticCandidate(
                type="preference",
                subject="用户",
                predicate="喜欢",
                object=object_,
                detail=f"用户喜欢{object_}。",
                importance=0.75,
                emotion_valence="positive",
                emotion_intensity=0.6,
                confidence=0.9,
            )
        ]
    )


class FakeExtractor:
    def __init__(self, *results: ExtractionResult) -> None:
        self.results = list(results)

    def extract(self, prompt: str) -> ExtractionResult:
        if not self.results:
            return ExtractionResult()
        return self.results.pop(0)


class ServerTestCase(unittest.TestCase):
    def start(self, extractor=None):
        self.tempdir = tempfile.TemporaryDirectory()
        self.server = make_server(
            self.tempdir.name, extractor=extractor, host="127.0.0.1", port=0
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app.close()
        self.tempdir.cleanup()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            exc.close()
            return exc.code, body


class ServerReadWriteTests(ServerTestCase):
    def setUp(self) -> None:
        self.start(FakeExtractor(_fact()))

    def tearDown(self) -> None:
        self.stop()

    def test_health(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_consolidate_then_inject(self) -> None:
        status, report = self.request(
            "POST",
            "/consolidate",
            {"peer_id": "u1", "messages": [{"role": "user", "content": "我喜欢白茶。"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(report["semantic_written"], 1)
        self.assertEqual(report["peer_id"], "u1")

        status, injection = self.request(
            "POST", "/inject", {"peer_id": "u1", "query": "我喜欢喝什么茶？"}
        )
        self.assertEqual(status, 200)
        self.assertIn("白茶", injection["context"])
        self.assertTrue(injection["memory_ids"])

    def test_peers_are_isolated(self) -> None:
        self.request(
            "POST",
            "/consolidate",
            {"peer_id": "alice", "messages": [{"role": "user", "content": "我喜欢白茶。"}]},
        )
        status, injection = self.request(
            "POST", "/inject", {"peer_id": "bob", "query": "我喜欢喝什么？"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(injection["memory_ids"], [])
        self.assertNotIn("白茶", injection["context"])

    def test_validation_errors(self) -> None:
        status, body = self.request("POST", "/consolidate", {"peer_id": "u1", "messages": []})
        self.assertEqual(status, 400)

        status, body = self.request("POST", "/inject", {"peer_id": "u1"})
        self.assertEqual(status, 400)

        status, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)

    def test_core_and_list_debug_endpoints(self) -> None:
        self.request(
            "POST",
            "/consolidate",
            {"peer_id": "u1", "messages": [{"role": "user", "content": "我喜欢白茶。"}]},
        )
        status, core = self.request("GET", "/core?peer_id=u1")
        self.assertEqual(status, 200)
        self.assertIn("version", core)

        status, listing = self.request("GET", "/list?peer_id=u1&kind=semantic")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["items"][0]["object"], "白茶")

    def test_stats_endpoint(self) -> None:
        self.request(
            "POST",
            "/consolidate",
            {"peer_id": "u1", "messages": [{"role": "user", "content": "我喜欢白茶。"}]},
        )
        status, stats = self.request("GET", "/stats?peer_id=u1")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(stats["total_memories"], 1)
        self.assertIn("archive_ratio", stats)
        self.assertIn("tiers", stats["semantic"])

    def test_forget_tombstone_and_hard_delete_approval(self) -> None:
        self.request(
            "POST",
            "/consolidate",
            {"peer_id": "u1", "messages": [{"role": "user", "content": "我喜欢白茶。"}]},
        )
        _, listing = self.request("GET", "/list?peer_id=u1&kind=semantic")
        memory_id = listing["items"][0]["id"]

        # 默认 P1 墓碑
        status, result = self.request("POST", "/forget", {"peer_id": "u1", "memory_id": memory_id})
        self.assertEqual(status, 200)
        self.assertEqual(result["operation"], "tombstone")

        # hard 未审批 → 403
        status, _ = self.request(
            "POST", "/forget", {"peer_id": "u1", "memory_id": memory_id, "hard": True}
        )
        self.assertEqual(status, 403)

        # hard + approved → 物理删除
        status, result = self.request(
            "POST",
            "/forget",
            {"peer_id": "u1", "memory_id": memory_id, "hard": True, "approved": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["operation"], "hard_delete")
        _, listing = self.request("GET", "/list?peer_id=u1&kind=semantic")
        self.assertEqual(listing["items"], [])


class ServerProactiveTests(ServerTestCase):
    def setUp(self) -> None:
        self.start(FakeExtractor(_fact()))

    def tearDown(self) -> None:
        self.stop()

    def test_proactive_ensure_and_status(self) -> None:
        # 未建立配置时默认不开口
        status, result = self.request("POST", "/proactive", {"peer_id": "u1", "local_hour": 14})
        self.assertEqual(status, 200)
        self.assertFalse(result["should"])
        # ensure 建配置
        status, result = self.request("POST", "/proactive", {"peer_id": "u1", "ensure": True, "local_hour": 14})
        self.assertEqual(status, 200)
        self.assertTrue(result["should"], result.get("reason"))
        self.assertIn("prompt", result)

    def test_proactive_quiet_hours_via_local_hour(self) -> None:
        self.request("POST", "/proactive", {"peer_id": "u1", "ensure": True})
        status, result = self.request("POST", "/proactive", {"peer_id": "u1", "local_hour": 23})
        self.assertEqual(status, 200)
        self.assertFalse(result["should"])
        self.assertEqual(result["reason"], "quiet_hours")

    def test_proactive_record(self) -> None:
        self.request("POST", "/proactive", {"peer_id": "u1", "ensure": True})
        status, result = self.request(
            "POST", "/proactive_record", {"peer_id": "u1", "umo": "aiocqhttp:FriendMessage:123"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["recorded"])
        # 记录后：min_interval 挡住
        status, result = self.request("POST", "/proactive", {"peer_id": "u1", "local_hour": 14})
        self.assertFalse(result["should"])
        self.assertEqual(result["reason"], "min_interval_not_reached")

    def test_proactive_invalid_local_hour(self) -> None:
        status, _ = self.request("POST", "/proactive", {"peer_id": "u1", "local_hour": 99})
        self.assertEqual(status, 400)


class ServerReadOnlyTests(ServerTestCase):
    def setUp(self) -> None:
        self.start(None)

    def tearDown(self) -> None:
        self.stop()

    def test_consolidate_requires_extractor_but_reads_work(self) -> None:
        status, body = self.request(
            "POST",
            "/consolidate",
            {"peer_id": "u1", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 503)

        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)

        status, injection = self.request(
            "POST", "/inject", {"peer_id": "u1", "query": "hi"}
        )
        self.assertEqual(status, 200)
        self.assertIn("context", injection)


if __name__ == "__main__":
    unittest.main()
