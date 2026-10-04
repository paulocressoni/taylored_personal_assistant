// frontend/src/lib/__tests__/voice.test.ts
// Unit tests for the pure voice helpers. The Web Audio graph itself is not
// covered here: jsdom has no AudioContext, which is exactly why the scheduling
// rule is a pure function that takes the clock as an argument.
import { describe, expect, it } from 'vitest'
import { formatLatency, nextPlaybackTime, pcm16ToFloat32 } from '../voice'

describe('pcm16ToFloat32', () => {
  it('maps the int16 range onto [-1, 1)', () => {
    const bytes = new Uint8Array([0x00, 0x00, 0xff, 0x7f, 0x00, 0x80])

    expect(Array.from(pcm16ToFloat32(bytes))).toEqual([0, 32767 / 32768, -1])
  })

  it('reads little-endian, per the wire contract', () => {
    // 0x01 0x02 is 0x0201 = 513, not 258.
    expect(pcm16ToFloat32(new Uint8Array([0x01, 0x02]))[0]).toBeCloseTo(513 / 32768, 6)
  })

  it('ignores an odd trailing byte instead of reading past the end', () => {
    expect(pcm16ToFloat32(new Uint8Array([0x7f, 0x00, 0x7f])).length).toBe(1)
  })

  it('honours the offset of a view into a larger buffer', () => {
    const backing = new Uint8Array([0xff, 0xff, 0x00, 0x40])

    expect(pcm16ToFloat32(backing.subarray(2))[0]).toBeCloseTo(0.5, 6)
  })

  it('returns nothing for an empty frame', () => {
    expect(pcm16ToFloat32(new Uint8Array()).length).toBe(0)
  })
})

describe('nextPlaybackTime', () => {
  it('queues back to back while the buffer still holds audio', () => {
    expect(nextPlaybackTime(10, 12)).toBe(12)
  })

  it('starts now when the queue has run dry', () => {
    expect(nextPlaybackTime(15, 12)).toBe(15)
  })
})

describe('formatLatency', () => {
  it('switches from milliseconds to seconds at one second', () => {
    expect(formatLatency(850)).toBe('850 ms')
    expect(formatLatency(999.6)).toBe('1.0 s')
    expect(formatLatency(3601.9)).toBe('3.6 s')
  })

  it('renders nothing when there is no measurement', () => {
    expect(formatLatency(null)).toBe('')
    expect(formatLatency(Number.NaN)).toBe('')
  })
})
