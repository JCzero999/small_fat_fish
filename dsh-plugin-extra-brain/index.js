// Cordis plugin that mounts the extra_brain memory onto DeepSeek Harness.
//
// Layout: this plugin is a thin adapter. The real memory logic lives in the Python
// sidecar (`extra_brain.server`); this file only (a) injects retrieved memory before
// each turn and (b) consolidates finished turns via the sidecar.
//
// Binding points were verified against the installed DSH host source:
//   - `session/event`      -> accumulate user/assistant messages; flush on `turn/end`
//   - `agent/pre-step`     -> inject memory into the model request (per turn, step 1)
//   - `agent/disposed`     -> flush any unsent messages at session end
//   - `createUserMessage`  -> from `@deepseek-ai/dsh-llm`
//   - `getOrCreateAnonymousUserId` -> stable per-user peer id (one memory db per human,
//     shared across DSH sessions). For QQ later, map the QQ user/group here instead.
//
// IMPORTANT — scoped events: DSH emits `session/event` on a per-session carrier scope
// and `agent/*` events on the agent scope, so a bundle-inserted plugin MUST subscribe
// through `ctx.inject(["sessions"]/["agents"], ...)`. A plain `ctx.on(...)` on the
// plugin's own root scope never fires for these (verified against `dsh-session.js` /
// `dsh-agent.js`; the shipped `dsh-deepseek-account.js` uses the same inject pattern).

import { appendFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { getOrCreateAnonymousUserId } from '@deepseek-ai/dsh-anonymous-user-id'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SidecarClient } from './sidecar.js'

export const name = 'extra-brain'

// --- temporary runtime trace (diagnostic only; remove after the cross-session test) ---
const TRACE_DIR = join(process.env.TEMP || process.env.TMP || '/tmp', 'eb-markers')
try { mkdirSync(TRACE_DIR, { recursive: true }) } catch {}
const _traced = new Set()
function trace(tag) {
  if (_traced.has(tag)) return
  _traced.add(tag)
  try { appendFileSync(join(TRACE_DIR, 'trace.txt'), `${new Date().toISOString()} ${tag}\n`) } catch {}
}
// -------------------------------------------------------------------------------------

/** Extract plain text from a `user/message` or `assistant/message` session event. */
function textOfMessage(event) {
  const data = event.data
  // user/message: event.data is the message; assistant/message: event.data.message is.
  const message = event.type === 'assistant/message' ? data?.message : data
  const content = message?.content
  if (!Array.isArray(content)) return ''
  let out = ''
  for (const part of content) {
    if (part && part.type === 'text' && typeof part.text === 'string') out += part.text
  }
  return out.trim()
}

export function apply(ctx, config = {}) {
  trace('apply')
  const client = new SidecarClient(config.baseUrl ?? 'http://127.0.0.1:8756')
  const consolidateEvery = config.consolidateEvery ?? 1
  // Stable peer id: one memory db per human, shared across sessions. Override with
  // config.peerId when you want explicit isolation (e.g. a fixed test id).
  const peerId = config.peerId ?? getOrCreateAnonymousUserId()

  ctx.logger?.info?.(`extra-brain: loaded (peerId=${peerId}, sidecar=${client.baseUrl}, consolidateEvery=${consolidateEvery})`)

  // session.id -> { lastUserText, pending: [{ role, content }] }. The buffer is per
  // session (so a turn is never split across sessions), but every consolidate/inject
  // targets the shared `peerId` above.
  const states = new Map()

  function stateOf(sessionId) {
    let state = states.get(sessionId)
    if (!state) {
      state = { lastUserText: '', pending: [] }
      states.set(sessionId, state)
    }
    return state
  }

  function flush(sessionId) {
    const state = states.get(sessionId)
    if (!state || state.pending.length === 0) return
    const messages = state.pending
    state.pending = []
    trace('consolidate')
    client.consolidate({ peer_id: peerId, messages, session_id: sessionId }).catch((error) => {
      ctx.logger?.warn?.(`extra-brain: consolidate failed (peerId=${peerId}): ${String(error)}`)
    })
  }

  // (1) Accumulate real user/assistant messages and consolidate on turn end.
  //     Subscribed on the sessions scope — the per-session carrier scope is a child of
  //     this scope, so `session/event` reaches it (a root-scope listener would not).
  ctx.inject(['sessions'], (scope) => {
    scope.on('session/event', (session, event) => {
      trace('session-event')
      const id = session.id
      if (event.type === 'user/message' && event.data?.source?.kind === 'user') {
        trace('user-message')
        const text = textOfMessage(event)
        if (text) {
          const state = stateOf(id)
          state.lastUserText = text
          state.pending.push({ role: 'user', content: text })
        }
      } else if (event.type === 'assistant/message') {
        const text = textOfMessage(event)
        if (text) stateOf(id).pending.push({ role: 'assistant', content: text })
      } else if (event.type === 'turn/end') {
        trace('turn-end')
        const state = states.get(id)
        if (state && state.pending.length >= consolidateEvery) flush(id)
      }
    })
  })

  // (2) Inject memory context once per turn (step 1), before the model's first step.
  // (3) Flush any unsent messages when the agent/session is disposed.
  ctx.inject(['agents'], (scope) => {
    scope.on('agent/pre-step', async ({ agent, step, signal }, next) => {
      trace('pre-step')
      const decision = await next()
      if (decision.kind === 'reject' || signal.aborted || step !== 1) return decision
      const state = states.get(agent.session.id)
      const query = state?.lastUserText
      if (!query) return decision
      let injection
      try {
        injection = await client.inject({ peer_id: peerId, query })
      } catch (error) {
        ctx.logger?.warn?.(`extra-brain: inject failed (peerId=${peerId}): ${String(error)}`)
        return decision
      }
      if (!injection?.context) return decision
      trace('inject')
      const memoryMessage = createUserMessage({
        content: [{ type: 'text', text: injection.context }],
        source: {
          kind: name,
          form: 'snapshot',
          sections: [{ name, text: injection.context }],
        },
      })
      return { ...decision, messages: [...decision.messages, memoryMessage] }
    })

    scope.on('agent/disposed', (...args) => {
      trace('disposed')
      // Payload shape varies across DSH builds: accept either ({ agent }) or
      // (carrier, "agent/disposed", { agent }).
      const payload = args.find((a) => a && typeof a === 'object' && a.agent)
      const agent = payload?.agent
      if (agent?.id) {
        flush(agent.id)
        states.delete(agent.id)
      }
    })
  })
}
