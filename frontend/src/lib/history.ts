// frontend/src/lib/history.ts
// Pure, framework-free logic for converting persisted backend messages into
// the bubbles the chat view renders. Kept OUT of the React hook on purpose:
// pure functions like this are trivial to unit-test (no React, no fetch).

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

// TODO(bug): re-opening a previous conversation can render TWO assistant
// bubbles for a single turn. The checkpointer persists every AI message the
// graph emitted — the knowledge-specialist's answer AND the responder's final
// message — and this mapper renders every `ai` message as a bubble. Only the
// LAST AI message of a turn (the responder) should be shown.
// historyToChatMessages is per-message today, so the fix needs list-level
// grouping: a new function that takes the full SessionHistory, walks turns
// (a 'human' message starts a turn), and keeps only the final 'ai' message of
// each turn. Add a unit test in src/lib/__tests__/history.test.ts.

// Convert one persisted backend message into chat bubbles.
// human -> user bubble, ai -> assistant bubble. tool/system messages are
// internal bookkeeping (tool results, system instructions) and are dropped —
// an empty array means "nothing to render", which callers can flatMap away.
export function historyToChatMessages(m: HistoryMessage): ChatMessage[] {
  if (m.role === 'human') {
    return [{ id: crypto.randomUUID(), role: 'user', content: m.content }]
  }
  if (m.role === 'ai') {
    return [{ id: crypto.randomUUID(), role: 'assistant', content: m.content }]
  }
  return []
}
