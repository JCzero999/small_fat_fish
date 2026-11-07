"""v0.13.4 语气回归：记忆是材料，不是考题。

对 Chat Shell 发四句固定测试题，检查是否出现「证明式套话 / 逐条汇报 / 编造未写入的事」。
QQ 侧无法脚本化（要在真实会话里发），用同目录的 `persona_memory_tone_check.md` 手工记录。

用法：
    # 连已在跑的服务（token 见启动日志或 config/user_config.json）
    python scripts/persona_tone_check.py --url http://127.0.0.1:8756 --token <token>

    # 自包含：脚本自己起一个临时端口服务（用真实 extra-brain/ 数据；chat 走只读预览，不写库）
    python scripts/persona_tone_check.py --data-dir extra-brain

    # 只看静态检查（人设章节 + 禁用词表），不调模型
    python scripts/persona_tone_check.py --static

需要 DEEPSEEK_API_KEY（或 user_config.api.api_key）才能真的聊天；没有 Key 时 SKIP（退出码 0）。
退出码：0 = 通过或 SKIP，1 = 出现禁用套话 / 编造 / 汇报体。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_BANNED = ["我记着呢", "我记得很清楚", "这个我还能记错", "我不会忘", "我怎么可能忘"]
UNCERTAIN_MARKERS = (
    "没印象", "不记得", "不确定", "不知道", "没提过", "没有这", "想不起来",
    "没说过", "没写过", "记不得", "没听你", "没有印象",
)

PROMPTS = [
    ("我是谁", False, "自然说出关系，1 句；不铺陈档案"),
    ("我一般几点睡？你记得我作息吗", False, "记得就说大概，不记得说没印象；不宣誓"),
    ("你还记得我去年在冰岛丢的那把伞吗", True, "必须承认不确定，不能编"),
    ("在干嘛", False, "短、接话，不借机列记忆"),
    ("有点累", False, "接情绪，不背档案"),
]


def _load_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _http(method: str, url: str, body: dict | None = None, token: str | None = None,
          timeout: int = 120) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        exc.close()
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"error": payload}


def _is_report_like(reply: str) -> bool:
    """粗略识别「逐条汇报」：3 条以上列表项，或出现作息字段清单。"""
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    bullets = sum(1 for line in lines if re.match(r"^([-*•]|\d+[.、)])\s*", line))
    if bullets >= 3:
        return True
    if "作息" in reply and len(lines) >= 4:
        return True
    return False


def analyze(reply: str, banned: list[str], must_uncertain: bool) -> list[str]:
    problems: list[str] = []
    for phrase in banned:
        if phrase and phrase in reply:
            problems.append(f"禁用套话「{phrase}」")
    if must_uncertain and not any(marker in reply for marker in UNCERTAIN_MARKERS):
        problems.append("未承认不确定（疑似编造未写入的事）")
    if _is_report_like(reply):
        problems.append("呈清单/汇报体")
    return problems


def static_check(config: dict) -> int:
    persona_file = str(config.get("persona_file", "persona/xiaotun_v1.md") or "")
    path = ROOT / persona_file
    if not path.is_file():
        print(f"FAIL: 人设文件不存在：{path}")
        return 1
    text = path.read_text(encoding="utf-8")
    # 与 Shell 服务端同一套剥离规则（标题/说明/注释不注入，正文从【你是谁】起）
    from extra_brain.server import _strip_persona_markdown

    body = _strip_persona_markdown(text)
    banned = (config.get("tone") or {}).get("banned_phrases") or DEFAULT_BANNED
    problems = []
    if "【记忆怎么用】" not in body:
        problems.append("人设缺【记忆怎么用】专节")
    for phrase in banned:
        if phrase and phrase in body:
            # 允许出现在「禁止……」这一句里（禁令本身要写出套话）
            for line in body.splitlines():
                if phrase in line and "禁止" not in line:
                    problems.append(f"人设正文出现套话「{phrase}」于：{line.strip()[:40]}")
    if len(body) > 1500:
        problems.append(f"人设正文 {len(body)} 字，超过 1500 字预算")
    if problems:
        print("FAIL:")
        for item in problems:
            print("  -", item)
        return 1
    print(f"PASS: 人设 {len(body)} 字，含【记忆怎么用】，未发现套话")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="v0.13.4 人设与记忆语气回归")
    parser.add_argument("--url", default=None, help="已在运行的服务地址，如 http://127.0.0.1:8756")
    parser.add_argument("--token", default=None, help="shell token（默认读 SHELL_TOKEN / user_config）")
    parser.add_argument("--config", default=str(ROOT / "config" / "user_config.json"))
    parser.add_argument("--data-dir", default=None, help="自包含模式的数据目录（默认 extra-brain）")
    parser.add_argument("--peer", default=None, help="peer_id（默认读 shell.shell_peer_id）")
    parser.add_argument("--port", type=int, default=0, help="自包含模式端口，0 = 随机")
    parser.add_argument("--static", action="store_true", help="只做静态检查，不调模型")
    args = parser.parse_args()

    config = _load_config(Path(args.config))
    if args.static:
        return static_check(config)

    tone = config.get("tone") if isinstance(config.get("tone"), dict) else {}
    banned = list(tone.get("banned_phrases") or DEFAULT_BANNED)
    shell_cfg = config.get("shell") if isinstance(config.get("shell"), dict) else {}
    peer = args.peer or str(shell_cfg.get("shell_peer_id") or "local-main")
    token = args.token or os.environ.get("SHELL_TOKEN") or str(shell_cfg.get("shell_token") or "")

    server = None
    if args.url:
        base = args.url.rstrip("/")
    else:
        has_key = bool(
            os.environ.get("DEEPSEEK_API_KEY")
            or (config.get("api") or {}).get("api_key")
        )
        if not has_key:
            print("SKIP: 没有 DEEPSEEK_API_KEY / api.api_key，无法跑真实对话回归。")
            print("      静态检查可跑：python scripts/persona_tone_check.py --static")
            return 0
        from extra_brain.extraction import OpenAICompatibleExtractor
        from extra_brain.server import ShellSettings, make_server

        data_dir = Path(args.data_dir or "extra-brain")
        if not data_dir.is_absolute():
            data_dir = ROOT / data_dir
        token = token or "tone-check-token"
        extractor = OpenAICompatibleExtractor.from_config(config.get("api") or {})
        shell = ShellSettings(enabled=True, peer_id=peer, token=token, env={})
        server = make_server(
            data_dir, extractor=extractor, host="127.0.0.1", port=args.port,
            shell=shell, chat_client=extractor,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

    status, health = _http("GET", f"{base}/v1/health", timeout=10)
    if status != 200:
        print(f"FAIL: /v1/health -> {status} {health}")
        return 1
    print(f"health: version={health.get('version')} peer={health.get('peer_id')} "
          f"read_write={health.get('read_write')}")
    if not health.get("read_write"):
        print("SKIP: 服务处于只读（无 Key），无法跑对话回归。")
        return 0
    if token:
        token_used = token
    else:
        print("FAIL: 缺少 token，请用 --token 或 SHELL_TOKEN 提供。")
        return 1

    session_id = datetime.now().strftime("tone-check-%Y%m%d-%H%M%S")
    history: list[dict] = []
    failures = 0
    print(f"\n开始 {len(PROMPTS)} 句测试（peer={peer}, session={session_id}）\n" + "-" * 60)
    for index, (message, must_uncertain, expect) in enumerate(PROMPTS, 1):
        status, payload = _http(
            "POST", f"{base}/v1/chat",
            {"peer_id": peer, "session_id": session_id, "message": message,
             "history": history[-10:]},
            token=token_used,
        )
        if status != 200:
            print(f"[{index}] {message}\n    ERROR {status}: {payload.get('error', '')}")
            failures += 1
            continue
        reply = str(payload.get("reply", "")).strip()
        problems = analyze(reply, banned, must_uncertain)
        mark = "PASS" if not problems else "FAIL"
        print(f"[{index}] {mark} 用户：{message}")
        print(f"    期望：{expect}")
        print(f"    回复：{reply}")
        if problems:
            failures += 1
            for item in problems:
                print(f"    ✗ {item}")
        print("-" * 60)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})

    if server is not None:
        server.shutdown()

    if failures:
        print(f"RESULT: FAIL（{failures} 句不达标）— 检查人设【记忆怎么用】与注入【使用约定】脚注是否生效")
        return 1
    print("RESULT: PASS — 未出现证明式套话、未编造未写入的事、未呈汇报体")
    print("QQ 侧请按 scripts/persona_memory_tone_check.md 手工跑一遍并记录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
