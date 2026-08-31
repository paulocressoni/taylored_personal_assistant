// frontend/src/lib/__tests__/sessions.test.ts
// Unit tests for the localStorage session registry.
//
// sessions.ts keeps NO module-level state: every call reads/writes
// localStorage directly. That makes tests trivially isolated — wipe
// localStorage before each test and we always start from a clean slate.
import { beforeEach, describe, expect, it } from 'vitest'
import {
  createSession,
  getActiveSessionId,
  listSessions,
  markActiveSession,
  removeSession,
} from '../sessions'

beforeEach(() => {
  localStorage.clear()
})

describe('getActiveSessionId', () => {
  it('creates a default session on first use', () => {
    const id = getActiveSessionId()
    expect(id).toBeTruthy()
    expect(listSessions()).toHaveLength(1)
    expect(listSessions()[0].id).toBe(id)
  })

  it('returns the persisted active id when it exists', () => {
    const s = createSession('Saved')
    expect(getActiveSessionId()).toBe(s.id)
  })
})

describe('createSession', () => {
  it('auto-names when no name is given and makes the new one active', () => {
    const first = createSession()
    expect(first.name).toBe('Conversation 1')
    expect(getActiveSessionId()).toBe(first.id)

    const second = createSession()
    expect(second.name).toBe('Conversation 2')
    expect(getActiveSessionId()).toBe(second.id)
  })

  it('uses the given name', () => {
    const s = createSession('Homework')
    expect(s.name).toBe('Homework')
  })
})

describe('markActiveSession', () => {
  it('switches the active session', () => {
    const a = createSession('A')
    createSession('B')
    markActiveSession(a.id)
    expect(getActiveSessionId()).toBe(a.id)
  })

  it('ignores unknown ids (no-op)', () => {
    const a = createSession('A')
    markActiveSession('does-not-exist')
    // active id is unchanged — still the session that was created
    expect(getActiveSessionId()).toBe(a.id)
  })
})

describe('removeSession', () => {
  it('removes a non-active session and keeps the active one', () => {
    const a = createSession('A')
    const b = createSession('B')
    markActiveSession(a.id) // active = A
    const next = removeSession(b.id) // delete B
    expect(next).toBe(a.id)
    expect(listSessions().map((s) => s.id)).toEqual([a.id])
  })

  it('falls back to the first remaining session when the active one is deleted', () => {
    const a = createSession('A')
    const b = createSession('B') // b is now active (last created)
    const next = removeSession(b.id)
    expect(next).toBe(a.id)
    expect(getActiveSessionId()).toBe(a.id)
  })

  it('creates a fresh default when the list becomes empty', () => {
    const a = createSession('Only')
    const next = removeSession(a.id)
    expect(next).toBeTruthy()
    expect(listSessions()).toHaveLength(1)
    expect(listSessions()[0].id).toBe(next)
  })
})
