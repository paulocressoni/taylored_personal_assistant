// frontend/src/lib/__tests__/history.test.ts
// Unit tests for the pure message-mapping helper. Pure functions like this
// need no mocks and no DOM — just call them and assert on the result.
import { describe, expect, it } from 'vitest'
import { sessionHistoryToChatMessages, type HistoryMessage } from '../history'

describe('sessionHistoryToChatMessages', () => {
  it('maps a human message to a user bubble', () => {
    const bubbles = sessionHistoryToChatMessages([{ role: 'human', content: 'hi' }])
    expect(bubbles.map((b) => [b.role, b.content])).toEqual([['user', 'hi']])
    expect(bubbles[0].id).toBeTruthy() // each bubble gets a fresh id
  })

  it('shows one user bubble and one assistant bubble for a normal turn', () => {
    const history: HistoryMessage[] = [
      { role: 'human', content: '2+2?' },
      { role: 'ai', content: '4' },
    ]
    expect(sessionHistoryToChatMessages(history).map((b) => [b.role, b.content])).toEqual([
      ['user', '2+2?'],
      ['assistant', '4'],
    ])
  })

  it('drops tool and system messages (internal bookkeeping)', () => {
    const history: HistoryMessage[] = [
      { role: 'system', content: 'you are a home assistant' },
      { role: 'human', content: '2+2?' },
      { role: 'tool', content: '{"result":"4"}' },
      { role: 'ai', content: '4' },
    ]
    expect(sessionHistoryToChatMessages(history)).toHaveLength(2) // user + assistant only
  })

  it('keeps only the LAST ai message of a turn — regression for duplicate bubbles', () => {
    const history: HistoryMessage[] = [
      { role: 'human', content: 'turn the lights on' },
      { role: 'ai', content: 'I can help with that.' }, // knowledge specialist (intermediate)
      { role: 'ai', content: 'Done — living room lights on.' }, // responder (final)
      { role: 'human', content: 'thanks' },
      { role: 'ai', content: 'You are welcome.' },
    ]
    expect(sessionHistoryToChatMessages(history).map((b) => [b.role, b.content])).toEqual([
      ['user', 'turn the lights on'],
      ['assistant', 'Done — living room lights on.'], // NOT the intermediate ai
      ['user', 'thanks'],
      ['assistant', 'You are welcome.'],
    ])
  })

  it('does not leak a reply across turns when a turn has no ai reply', () => {
    const history: HistoryMessage[] = [
      { role: 'human', content: 'hi' },
      { role: 'ai', content: 'hello' },
      { role: 'human', content: '...and again' }, // this turn ends with no ai
    ]
    expect(sessionHistoryToChatMessages(history).map((b) => b.role)).toEqual([
      'user',
      'assistant',
      'user',
    ])
  })

  it('renders a leading assistant message (no preceding human)', () => {
    const history: HistoryMessage[] = [{ role: 'ai', content: 'Welcome back!' }]
    expect(sessionHistoryToChatMessages(history).map((b) => b.content)).toEqual(['Welcome back!'])
  })
})
