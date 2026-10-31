// Proves the Node/TS -> Python extra_brain bridge over HTTP, end to end.
//
// Usage (two terminals, or see README):
//   terminal 1:  export DEEPSEEK_API_KEY=... && python -m extra_brain.server --data-dir /tmp/eb --port 8756
//   terminal 2:  node scripts/bridge-smoke.mjs [baseUrl]
//
// Consolidates one turn then injects a query, printing what the memory "remembers".

const base = process.argv[2] ?? 'http://127.0.0.1:8756'

async function post(path, body) {
  const resp = await fetch(base + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(`${path} ${resp.status} ${data.error ?? ''}`)
  return data
}

const peer = `bridge-smoke-${Date.now()}`

const rep = await post('/consolidate', {
  peer_id: peer,
  messages: [{ role: 'user', content: '以后叫我小舟吧，我最怕面试环节了。' }],
})
console.log('consolidate ->', {
  semantic_written: rep.semantic_written,
  episodic_written: rep.episodic_written,
  conflicts: rep.conflicts.length,
  core_pruned: rep.core_pruned.length,
})

const inj = await post('/inject', {
  peer_id: peer,
  query: '你还记得我叫什么、最怕什么吗？',
})
console.log('inject ->', {
  tokens: inj.estimated_tokens,
  memory_ids: inj.memory_ids,
})
console.log('context head:\n' + inj.context.slice(0, 260))
