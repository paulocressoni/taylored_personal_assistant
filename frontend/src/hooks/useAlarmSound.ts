// frontend/src/hooks/useAlarmSound.ts
import { useCallback, useRef } from 'react'

/**
 * Scaffold for M17 (alarms). NOT wired into the UI yet.
 *
 * Uses the Web Audio API to synthesize a tone — no audio files needed.
 * M17 will call play()/stop() when an alarm should ring on this device
 * (identified by the device_id from lib/deviceId.ts).
 */
export function useAlarmSound() {
  const ctxRef = useRef<AudioContext | null>(null)
  const oscRef = useRef<OscillatorNode | null>(null)

  // Browsers require a user gesture before audio can start, so the context
  // is created lazily on first play() rather than at component mount.
  const ensureCtx = useCallback((): AudioContext | null => {
    if (typeof window === 'undefined') return null
    if (!ctxRef.current) {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Ctor) return null
      ctxRef.current = new Ctor()
    }
    return ctxRef.current
  }, [])

  // Tear down whatever is currently ringing. stop() is guarded — calling it
  // on an already-stopped oscillator throws in some browsers — and
  // disconnect() releases the node so the Web Audio graph can be collected.
  const stopCurrent = useCallback(() => {
    const osc = oscRef.current
    if (!osc) return
    try {
      osc.stop()
    } catch {
      // Already stopped / never started — nothing to tear down.
    }
    osc.disconnect()
    oscRef.current = null
  }, [])

  const play = useCallback(() => {
    const ctx = ensureCtx()
    if (!ctx) return
    if (ctx.state === 'suspended') void ctx.resume()

    // Never stack tones. Each play() makes a fresh oscillator, but if
    // the previous one is still sounding it must be stopped + disconnected
    // FIRST — otherwise rapid play() calls leak active nodes until each one's
    // own 1-second stop() fires.
    stopCurrent()

    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.value = 880 // A5 — a clean alarm tone
    gain.gain.value = 0.2
    osc.connect(gain).connect(ctx.destination)
    osc.start()
    osc.stop(ctx.currentTime + 1) // ring for 1 second
    oscRef.current = osc
  }, [ensureCtx, stopCurrent])

  const stop = useCallback(() => {
    stopCurrent()
  }, [stopCurrent])

  return { play, stop }
}
