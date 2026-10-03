// frontend/src/lib/voice.ts
// Pure helpers for the voice client: PCM decoding for playback, the gapless
// scheduling rule, and latency formatting. Everything stateful — the
// AudioContexts, the playback cursor, the socket — lives in
// hooks/useVoiceSession.ts, and pure logic lives here so it can be tested
// without a browser audio stack.
//
// Two deliberate omissions. Capture-side conversion lives in the AudioWorklet
// (public/worklets/pcm-capture.js), which runs in its own global scope and
// cannot import this module. And there is NO client-side VAD: the server owns
// endpointing so the browser and the hardware boundary audio identically, and
// the client learns that speech started from the `speech_start` frame instead.

/**
 * Convert little-endian signed 16-bit PCM into float samples in [-1, 1).
 *
 * The inverse of the backend's `to_pcm16`, so a round trip through the pipeline
 * is stable. Reads through a DataView carrying the array's own offset, because a
 * socket frame can be a view into a larger buffer.
 *
 * @param bytes - Raw PCM exactly as it arrived on the socket.
 * @returns One float sample per int16; an odd trailing byte is ignored.
 */
export function pcm16ToFloat32(bytes: Uint8Array): Float32Array {
  const samples = new Float32Array(Math.floor(bytes.length / 2))
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = view.getInt16(index * 2, true) / 32768
  }
  return samples
}

/**
 * Return the context time to schedule the next audio chunk at.
 *
 * This one line is what makes playback gapless. Chunks arrive faster than
 * realtime, so each is queued at the END of the previous one, and every chunk
 * starts exactly where the last stopped — no seam, no click. When the queue has
 * run dry we start "now" instead: a time in the past is dropped silently by
 * `start()`, which would swallow the beginning of the answer.
 *
 * @param contextTime - `AudioContext.currentTime`, right now.
 * @param previousEndTime - When the previously scheduled chunk ends; 0 before any.
 * @returns The absolute context time to start the next chunk at.
 */
export function nextPlaybackTime(contextTime: number, previousEndTime: number): number {
  return Math.max(contextTime, previousEndTime)
}

/**
 * Format a latency measurement for display.
 *
 * @param milliseconds - The measurement in ms, or null when there is none.
 * @returns "850 ms", "3.6 s", or an empty string when unmeasurable.
 */
export function formatLatency(milliseconds: number | null): string {
  if (milliseconds === null || !Number.isFinite(milliseconds)) return ''
  const rounded = Math.round(milliseconds)
  return rounded < 1000 ? `${rounded} ms` : `${(rounded / 1000).toFixed(1)} s`
}
