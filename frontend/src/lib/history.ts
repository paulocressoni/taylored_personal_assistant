// frontend/src/lib/history.ts
// Pure, framework-free logic for converting persisted backend messages into
// the bubbles the chat view renders. Kept OUT of the React hook on purpose:
// pure functions like this are trivial to unit-test (no React, no fetch).

import { newUuid } from './uuid'

// The shape of one bubble in the chat view.
export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
}

// The shape of one message returned by GET /sessions/{id}/history
// (mirrors the backend SessionHistoryResponse schema).
export type HistoryMessage = { role: string; content: string }

export type SessionHistory = { session_id: string; messages: HistoryMessage[] }

// Convert a persisted session history into chat bubbles.
//
// List-level on PURPOSE, not per-message: the checkpointer persists every ai
// message the LangGraph emitted for one turn (the knowledge-specialist's
// answer AND the responder's final message). Rendering each one via flatMap
// produced two assistant bubbles per turn. Rule: a 'human' message starts a
// turn, and only the LAST 'ai' message of that turn is the reply worth
// showing. tool/system messages are internal bookkeeping — dropped, and they
// do NOT reset the current turn.
export function sessionHistoryToChatMessages(messages: HistoryMessage[]): ChatMessage[] {
  const bubbles: ChatMessage[] = []
  let pendingReply: HistoryMessage | null = null // last ai seen in the current turn

  const flushReply = () => {
    if (pendingReply) {
      bubbles.push({
        id: newUuid(),
        role: 'assistant',
        content: pendingReply.content,
      })
      pendingReply = null
    }
  }

  for (const m of messages) {
    if (m.role === 'human') {
      flushReply() // previous turn is over -> emit only its final ai reply
      bubbles.push({ id: newUuid(), role: 'user', content: m.content })
    } else if (m.role === 'ai') {
      pendingReply = m // overwrite: only the LAST ai of this turn survives
    }
    // tool / system: ignored
  }

  flushReply() // trailing ai with no following human (e.g. a greeting)
  return bubbles
}
