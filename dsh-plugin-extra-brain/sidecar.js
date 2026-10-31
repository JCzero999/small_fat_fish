// HTTP client for the extra_brain Python sidecar.
//
// No runtime dependencies: uses global `fetch` (Node 18+). This module is fully
// correct and testable independently of DSH — it only speaks JSON over HTTP to the
// Python `extra_brain.server` process.

export class SidecarClient {
  constructor(baseUrl) {
    this.baseUrl = baseUrl
  }

  async inject(input) {
    return this.post('/inject', input)
  }

  async consolidate(input) {
    return this.post('/consolidate', input)
  }

  async post(path, body) {
    let response
    try {
      response = await fetch(this.baseUrl + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch (cause) {
      throw new Error(`extra_brain sidecar unreachable at ${this.baseUrl}: ${String(cause)}`)
    }
    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
      throw new Error(`extra_brain ${path} failed: ${response.status} ${data.error ?? ''}`)
    }
    return data
  }
}
