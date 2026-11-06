"""v0.13.0 Chat Shell 无 GUI 回归脚本（迭代文档 §3 T6）。

流程：
  1. health -> ok
  2. chat 用户：「我叫小舟，请记住我下周三相亲面试会紧张」
  3. chat 再两轮闲聊
  4. consolidate 该 session
  5. 新 session_id 再 chat：「你还记得我面试的事吗？」
  6. 断言 reply 或 inject_preview 文本中含「面试」或「紧张」或「小舟」之一

用法：
    python scripts/shell_regression.py                # 临时目录 + 随机端口，自包含
    python scripts/shell_regression.py --keep-db DIR  # 指定 data-dir（调试用）

需要 DEEPSEEK_API_KEY（chat/consolidate 走真实 LLM）；无 Key 时 SKIP（退出码 0）。
退出码：0 = 通过或 skip，1 = 失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extra_brain.extraction import OpenAICompatibleExtractor  # noqa: E402
from extra_brain.server import ShellSettings, make_server  # noqa: E402

PEER = "local-main"
TOKEN = "regression-token"
KEYWORDS = ("面试", "紧张", "小舟")


def http_request(method: str, url: str, body: dict | None = None, token: str | None = TOKEN):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        exc.close()
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"error": payload}


def main() -> int:
    parser = argparse.ArgumentParser(description="v0.13.0 Chat Shell 回归")
    parser.add_argument("--keep-db", default=None, help="持久化 data 目录（默认临时目录）")
    parser.add_argument("--port", type=int, default=0, help="0 = 随机空闲端口")
    args = parser.parse_args()

    has_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    if not has_key:
        print("SKIP: DEEPSEEK_API_KEY not set — chat/consolidate need real LLM access.")
        return 0

    data_dir = args.keep_db or tempfile.mkdtemp(prefix="shell_reg_")
    extractor = OpenAICompatibleExtractor.from_env()
    shell = ShellSettings(enabled=True, peer_id=PEER, token=TOKEN, env={})
    server = make_server(
        data_dir, extractor=extractor, host="127.0.0.1", port=args.port,
        shell=shell, chat_client=extractor,
    )
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"shell regression server: {base} (data: {data_dir})")

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        # 1. health
        status, body = http_request("GET", f"{base}/v1/health", token=None)
        check("1. health ok", status == 200 and body.get("ok") is True, json.dumps(body, ensure_ascii=False))

        session = "shell-regression-001"
        history: list[dict[str, str]] = []

        def chat(message: str, sid: str) -> dict:
            nonlocal history
            status, body = http_request("POST", f"{base}/v1/chat", {
                "peer_id": PEER, "session_id": sid,
                "message": message, "history": history, "auto_inject": True,
            })
            if status != 200:
                raise RuntimeError(f"chat {status}: {body}")
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": body["reply"]})
            return body

        # 2. 核心记忆句
        reply1 = chat("我叫小舟，请记住我下周三相亲面试会紧张", session)
        print(f"  小豚: {reply1['reply'][:80]}")
        check("2. chat #1 injected", reply1.get("injected") is True)

        # 3. 两轮闲聊
        reply2 = chat("今天天气挺好的，你那边呢？", session)
        print(f"  小豚: {reply2['reply'][:80]}")
        reply3 = chat("对了，最近在听什么歌吗？", session)
        print(f"  小豚: {reply3['reply'][:80]}")
        check("3. chat #2 #3 ok", bool(reply2.get("reply")) and bool(reply3.get("reply")))

        # 4. consolidate
        status, report = http_request("POST", f"{base}/v1/consolidate", {
            "peer_id": PEER, "session_id": session, "messages": history,
        })
        check(
            "4. consolidate written",
            status == 200 and (report.get("semantic_written", 0) + report.get("episodic_written", 0)) > 0,
            json.dumps(report, ensure_ascii=False)[:300] if status != 200 else "semantic=%s episodic=%s" % (
                report.get("semantic_written"), report.get("episodic_written")),
        )
        print(f"  巩固报告: semantic={report.get('semantic_written')} episodic={report.get('episodic_written')} "
              f"conflicts={len(report.get('conflicts') or [])}")

        # 5. 新 session 召回
        new_session = "shell-regression-002"
        status, preview = http_request(
            "GET", f"{base}/v1/inject_preview?q={urllib.parse.quote('你还记得我面试的事吗？')}&peer_id={PEER}"
        )
        reply4 = chat("你还记得我面试的事吗？", new_session)
        print(f"  小豚(新会话): {reply4['reply'][:80]}")

        # 6. 断言关键词（reply 或 inject_preview 文本）
        recall_text = reply4["reply"] + "\n" + (preview.get("text", "") if status == 200 else "")
        hit = any(k in recall_text for k in KEYWORDS)
        check("5. inject_preview ok", status == 200 and bool(preview.get("text")), json.dumps(preview, ensure_ascii=False)[:200])
        check("6. cross-session recall hit keyword", hit, recall_text[:200])
    except Exception as exc:  # noqa: BLE001
        failures.append(f"exception: {exc}")
        print(f"  [FAIL] exception — {type(exc).__name__}: {exc}")
    finally:
        server.shutdown()
        server.server_close()
        server.app.close()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("ALL PASSED — 无 GUI 闭环（记住 -> 巩固 -> 新会话召回）验证通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
