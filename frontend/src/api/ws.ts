/**
 * WebSocket message types for /ws/chat.
 *
 * Hand-written (NOT generated) because OpenAPI does not describe WebSocket
 * traffic. Mirror the exact JSON frames the backend sends in
 * backend/app/api/routes.py chat_ws().
 */

export type WsStatusFrame = { type: 'status'; detail: string }
export type WsTokenFrame = { type: 'token'; content: string }
export type WsErrorFrame = { type: 'error'; detail: string }
export type WsDoneFrame = {
  type: 'done'
  reply: string
  lang: string | null
  route: string | null
  complete: boolean
}

// Union of every frame the server can send. The switch in the hook narrows
// this union by `type`, which gives us compile-time safety per frame.
export type WsFrame = WsStatusFrame | WsTokenFrame | WsErrorFrame | WsDoneFrame
