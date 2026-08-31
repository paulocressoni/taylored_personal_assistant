// frontend/src/lib/__tests__/history.test.ts
// Unit tests for the pure message-mapping helper. Pure functions like this
// need no mocks and no DOM — just call them and assert on the result.
import { describe, expect, it } from 'vitest'
import { historyToChatMessages, type HistoryMessage } from '../history'

describe('historyToChatMessages', () => {
  it('maps a human message to a user bubble', () => {
    const [msg] = historyToChatMessages({ role: 'human', content: 'hi' })
    expect(msg.role).toBe('user')
    expect(msg.content).toBe('hi')
    expect(msg.id).toBeTruthy() // each bubble gets a fresh id
  })

  it('maps an ai message to an assistant bubble', () => {
    const [msg] = historyToChatMessages({ role: 'ai', content: 'hello!' })
    expect(msg.role).toBe('assistant')
    expect(msg.content).toBe('hello!')
  })

  it('drops tool and system messages (internal bookkeeping)', () => {
    const tool: HistoryMessage = { role: 'tool', content: '{"result":"5"}' }
    const system: HistoryMessage = { role: 'system', content: 'you are...' }
    expect(historyToChatMessages(tool)).toEqual([])
    expect(historyToChatMessages(system)).toEqual([])
  })

  it('maps a mixed history to an ordered list of bubbles', () => {
    const history: HistoryMessage[] = [
      { role: 'human', content: '2+2?' },
      { role: 'tool', content: '{"result":"4"}' },
      { role: 'ai', content: '4' },
    ]
    const bubbles = history.flatMap(historyToChatMessages)
    expect(bubbles.map((b) => [b.role, b.content])).toEqual([
      ['user', '2+2?'],
      ['assistant', '4'],
    ])
  })
})
