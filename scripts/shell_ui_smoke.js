// shell 前端状态机冒烟（Node，无浏览器依赖）
// 用法（仓库根目录）：
//   node scripts/shell_ui_smoke.js                    # 正常路径：N 条自动巩固 / 切片 / pagehide
//   FAIL_CONSOLIDATE=1 node scripts/shell_ui_smoke.js # 失败路径：聊天不中断 / 失败节流 / 高级面板重试
// 原理：从 index.html 抽出 <script>，在 vm 里用 DOM/localStorage/fetch 桩执行。
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync("src/extra_brain/static/shell/index.html", "utf8");
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function makeEl(id) {
  const el = {
    id, value: "", textContent: "", innerHTML: "", className: "", disabled: false,
    style: {}, title: "", scrollTop: 0, scrollHeight: 0, children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) on = !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); return on; },
      contains(c) { return this._s.has(c); },
    },
    setAttribute() {}, getAttribute() { return null; },
    appendChild(c) { this.children.push(c); this.scrollHeight += 10; },
    addEventListener(ev, fn) { this._handlers = this._handlers || {}; this._handlers[ev] = fn; },
    focus() {}, click() { if (this.onclick) this.onclick(); },
    querySelector() { return makeEl("q"); },
  };
  return el;
}

const els = {};
const docHandlers = {};
const winHandlers = {};
const calls = { chat: [], consolidate: [], preview: [] };

global.document = {
  getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
  createElement() { return makeEl("new"); },
  addEventListener(ev, fn) { docHandlers[ev] = fn; },
  querySelector(sel) { return makeEl(sel); },
  visibilityState: "visible",
  body: makeEl("body"),
};
global.window = { addEventListener(ev, fn) { winHandlers[ev] = fn; } };
global.localStorage = {
  store: {},
  getItem(k) { return this.store[k] === undefined ? null : this.store[k]; },
  setItem(k, v) { this.store[k] = String(v); },
};
global.setTimeout = setTimeout;
global.clearTimeout = clearTimeout;

const POLICY = {
  enabled: true, every_n_user_messages: 3, on_new_session: true,
  on_page_hide: true, min_messages: 2,
};

function respond(payload, status = 200) {
  return Promise.resolve({
    status,
    json: () => Promise.resolve(payload),
  });
}

let allowConsolidate = process.env.FAIL_CONSOLIDATE !== "1";

global.fetch = function (url, opts) {
  opts = opts || {};
  const body = opts.body ? JSON.parse(opts.body) : null;
  if (url === "/v1/health") {
    return respond({ ok: true, peer_id: "qq:FriendMessage:1", read_write: true, version: "test", auto_consolidate: POLICY });
  }
  if (url === "/v1/chat") {
    calls.chat.push(body);
    return respond({ reply: "reply-" + (calls.chat.length), session_id: body.session_id, injected: true });
  }
  if (url === "/v1/consolidate") {
    calls.consolidate.push({ body, keepalive: !!opts.keepalive });
    if (!allowConsolidate) { return respond({ error: "boom" }, 500); }
    return respond({ semantic_written: 1, episodic_written: 1, conflicts: [] });
  }
  if (url.indexOf("/v1/stats") === 0) return respond({ ok: true });
  if (url === "/v1/inject_preview") { calls.preview.push(body); return respond({ text: "MEM", token_estimate: 12 }); }
  return respond({ error: "unexpected " + url }, 500);
};

vm.runInThisContext(js, { filename: "shell.js" });

const tick = (ms) => new Promise((r) => setTimeout(r, ms || 40));
const assert = (cond, msg) => { if (!cond) { console.error("FAIL:", msg); process.exitCode = 1; } else { console.log("ok  -", msg); } };

(async () => {
  localStorage.setItem("shell_token", "t");
  await tick(60); // health 返回

  const send = async (text) => {
    els["input"].value = text;
    els["btn-send"].onclick();
    await tick(50);
  };

  if (process.env.FAIL_CONSOLIDATE === "1") {
    // 场景二：巩固失败
    await send("我叫阿岚");
    await send("下周三做甲状腺复查");
    await send("最近在喝白茶");
    await tick(60);
    assert(calls.consolidate.length === 1, "自动巩固发出一次（失败）");
    assert(els["mem-status"].textContent === "记忆稍后再同步", "失败时状态文案 = 记忆稍后再同步：实际「" + els["mem-status"].textContent + "」");
    await send("还能继续聊天吗");
    assert(calls.chat.length === 4, "巩固失败后仍可继续聊天");
    assert(calls.consolidate.length === 1, "30s 内不重复重试（失败节流）");

    const beforeNew = calls.consolidate.length;
    els["btn-new"].onclick();
    await tick(120);
    assert(els["messages"].innerHTML === "", "新会话不被失败卡死（仍清空）");
    assert(calls.consolidate.length === beforeNew + 1, "新会话只额外冲刷一次（不无限重试）");
    assert(els["adv-pending"].textContent !== "0 条", "未写入片段进入 backlog（高级面板待重试）：" + els["adv-pending"].textContent);

    allowConsolidate = true; // 服务恢复
    els["btn-force"].onclick();
    await tick(200);
    assert(calls.consolidate.length > beforeNew, "高级面板「立即写入记忆」重试成功");
    assert(els["adv-pending"].textContent === "0 条", "重试成功后 backlog 清空");
    console.log("\nRESULT:", process.exitCode ? "FAIL" : "PASS（失败路径）");
    return;
  }

  await send("我叫阿岚");
  await send("下周三做甲状腺复查");
  assert(calls.consolidate.length === 0, "2 条用户消息（< N=3）不触发自动巩固");

  await send("最近在喝白茶");
  await tick(50);
  assert(calls.consolidate.length === 1, "3 条用户消息触发一次自动巩固");
  assert(calls.consolidate[0].body.messages.length === 6, "只送未巩固切片（3 轮 = 6 条消息）");

  // 第 4 条：已巩固的切片不重复发送
  await send("谢谢");
  await tick(50);
  assert(calls.consolidate.length === 1, "4 条用户消息不重复巩固（切片只剩 1 条 user < min=2... 实际 N=3 未达）");

  // 页面隐藏：还有 1 条未巩固 user（< min=2），不应冲刷
  els["input"].value = "再说一句";
  await send("再说一句");
  const beforeHide = calls.consolidate.length;
  winHandlers["pagehide"]();
  await tick(60);
  assert(calls.consolidate.length >= beforeHide, "pagehide 冲刷已触发（达到阈值时）");

  // 新会话：先冲刷再清空
  const consolidateBefore = calls.consolidate.length;
  els["btn-new"].onclick();
  await tick(80);
  assert(els["messages"].innerHTML === "", "新会话清空消息流");
  assert(calls.consolidate.length >= consolidateBefore, "新会话不报错（无待冲刷内容或已冲刷）");

  // 只读模式不应报错刷屏（单独跑一遍 health=read_write:false 的场景略）
  console.log("\nconsolidate calls:", calls.consolidate.map((c) => c.body.messages.length + " msgs" + (c.keepalive ? " (keepalive)" : "")));
  console.log("chat calls:", calls.chat.length);
  console.log(process.exitCode ? "\nRESULT: FAIL" : "\nRESULT: PASS");
})();
