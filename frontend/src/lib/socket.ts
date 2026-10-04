// frontend/src/lib/socket.ts
// The ONE place this app knows how to reach the backend: the shared API key, the
// auth header for HTTP calls, the WebSocket URL, and the close-code vocabulary.
// Both hooks (chat and voice) use it, so the "the key never travels in the URL"
// decision is made once.

/**
 * Return the shared API key from the Vite environment.
 *
 * @returns The key, or an empty string when VITE_API_KEY is not configured.
 */
export function apiKey(): string {
  return import.meta.env.VITE_API_KEY ?? ''
}

// The backend requires a shared API key. Read once from Vite env and send it two
// ways: X-API-Key on the session HTTP calls, and as the WebSocket subprotocol
// (Sec-WebSocket-Protocol) so it never lands in the URL / proxy / access logs.
// Browsers can't set headers on a WS handshake, but they CAN pass subprotocols.

/**
 * Auth header for the session HTTP calls.
 *
 * The return type is annotated on purpose: without it, the `{}` branch of the
 * ternary would infer as `{ 'X-API-Key'?: undefined }`, which fails fetch's
 * HeadersInit check.
 *
 * @returns A HeadersInit carrying X-API-Key, or an empty object without a key.
 */
export function authHeaders(): HeadersInit {
  const key = apiKey()
  return key ? { 'X-API-Key': key } : {}
}

/**
 * Absolute WebSocket URL for a backend route, resolved against the current page.
 *
 * Relative to the Vite dev server, which proxies /ws to the backend. The key is
 * NOT part of the URL — it travels as a subprotocol instead.
 *
 * @param path - Route path, e.g. "/ws/chat" or "/ws/voice".
 * @returns The absolute ws:// or wss:// URL for this host.
 */
export function wsUrl(path: string): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}${path}`
}

/**
 * Translate a WebSocket close code into a user-facing message.
 *
 * These are the codes the backend actually uses:
 *   1008 auth failed or the feature is off, 1003 invalid payload,
 *   1013 rate-limited / busy / timeout, 1011 internal error.
 * 1006 = the connection never completed (backend down) -> generic message.
 * 1000 / 1005 (normal) and anything else -> null, meaning "no message needed".
 *
 * @param code - The CloseEvent code.
 * @returns A message, or null when the close needs no explanation.
 */
export function wsErrorMessage(code: number): string | null {
  switch (code) {
    case 1008:
      return 'Authentication failed — check VITE_API_KEY.'
    case 1003:
      return 'The server rejected this message (invalid payload).'
    case 1013:
      return 'The assistant is busy — please try again in a moment.'
    case 1011:
      return 'The assistant hit an internal error.'
    case 1006:
      return 'Connection failed — is the backend running?'
    default:
      return null
  }
}
