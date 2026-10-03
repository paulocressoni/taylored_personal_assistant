// frontend/public/worklets/pcm-capture.js
// Capture-side AudioWorklet for /ws/voice: microphone float samples in, fixed
// 20 ms frames of little-endian int16 out, posted as transferable ArrayBuffers.
//
// Why an AudioWorklet and raw PCM: the backend image has NO ffmpeg, so a
// MediaRecorder / webm / opus upload is not an option — the wire contract is mono
// s16le at the context's own rate. Why public/: a worklet runs in its own global
// scope, loaded by URL from addModule(), so it cannot import from src/ — this file
// is deliberately self-contained.
//
// The node emits frames for as long as it is connected, one 128-sample render
// quantum at a time, accumulated into a full frame. Silence therefore keeps
// flowing, which is deliberate: a client that sends nothing for
// voice_idle_timeout_seconds (60 s) is dropped by the server, and the server's VAD
// treats a run of zeros as the absence of speech.
const FRAME_MS = 20
// 20 ms expressed in samples at whatever rate this context runs at (16 kHz for
// capture): `sampleRate` is a global in the AudioWorkletGlobalScope.
const FRAME_SAMPLES = Math.round(sampleRate * (FRAME_MS / 1000))

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.frame = new Int16Array(FRAME_SAMPLES)
    this.filled = 0
  }

  process(inputs) {
    const channel = inputs[0]?.[0]
    // Nothing connected yet: stay alive and wait for audio.
    if (!channel) return true

    for (const sample of channel) {
      // Clamp before scaling — a sample outside [-1, 1] would wrap around in the
      // int16 cast and come back as loud noise. Rounding matches the backend's
      // own to_pcm16, so a round trip through the pipeline is stable.
      const clamped = Math.max(-1, Math.min(1, sample))
      this.frame[this.filled] = Math.round(clamped * 32767)
      this.filled += 1

      if (this.filled === FRAME_SAMPLES) {
        // Transferred, not copied: the buffer leaves this scope, so allocate the
        // next frame rather than reuse memory the main thread now owns.
        this.port.postMessage(this.frame.buffer, [this.frame.buffer])
        this.frame = new Int16Array(FRAME_SAMPLES)
        this.filled = 0
      }
    }

    return true
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor)
