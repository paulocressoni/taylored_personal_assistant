// frontend/src/lib/sessions.ts
// The browser's registry of conversations ("sessions"), persisted in
// localStorage so it survives page reloads and browser restarts.

import { newUuid } from './uuid'

const STORAGE_KEY = 'ipa.sessions'
const ACTIVE_KEY = 'ipa.active_session_id'

// One conversation entry. `id` is the LangGraph "thread_id" we send to the
// backend on every message. Two conversations differ ONLY by their id —
// that is what makes the backend store (and recall) separate histories.
export type Session = {
  id: string
  name: string
  createdAt: number // epoch ms, so the UI could sort / show "created when"
}

// Read the persisted list (or [] if nothing stored yet).
// try/catch: if the stored text is corrupted we fail open with an empty list
// instead of crashing the app at startup.
function loadSessions(): Session[] {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) return []
  try {
    return JSON.parse(raw) as Session[]
  } catch {
    return []
  }
}

// Write the list back to localStorage.
function saveSessions(sessions: Session[]): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions))
}

// Remember which session is currently open.
function persistActive(id: string): void {
  localStorage.setItem(ACTIVE_KEY, id)
}

// --- Public API (what the hook / components use) --------------------------

// Return the full session list.
export function listSessions(): Session[] {
  return loadSessions()
}

// Create a NEW conversation: fresh id (=> fresh thread_id => no shared memory
// server-side), make it active, and return it so the caller can update UI.
export function createSession(name?: string): Session {
  const session: Session = {
    id: newUuid(),
    name: name || `Conversation ${loadSessions().length + 1}`,
    createdAt: Date.now(),
  }
  saveSessions([...loadSessions(), session])
  persistActive(session.id)
  return session
}

// Return which session is active. On the very first visit there is none, so
// we lazily create a default one (this replaces the old hardcoded 'default').
export function getActiveSessionId(): string {
  const active = localStorage.getItem(ACTIVE_KEY)
  const sessions = loadSessions()
  if (active && sessions.some((s) => s.id === active)) return active
  const session = sessions[0] ?? createSession() // reuse first, else create
  persistActive(session.id)
  return session.id
}

// Set the active session (when the user clicks a row in the list).
export function markActiveSession(id: string): void {
  if (loadSessions().some((s) => s.id === id)) persistActive(id)
}

// Delete a session from the registry. Returns the id that should become
// active afterwards (the next one, or a fresh default if the list is empty).
// NOTE: this only "forgets" it in the browser. Phase 2 will also call
// DELETE /sessions/{id} on the backend to erase the stored conversation.
export function removeSession(id: string): string {
  const remaining = loadSessions().filter((s) => s.id !== id)
  saveSessions(remaining)

  // Decide the next active id:
  //  - deleted the active one      -> fall back to the first remaining
  //  - deleted a non-active one    -> keep whatever is already active
  //  - list is now empty           -> create a fresh default conversation
  let next = localStorage.getItem(ACTIVE_KEY) ?? ''
  if (next === id) next = remaining[0]?.id ?? ''
  if (!next) next = createSession().id
  persistActive(next)
  return next
}
