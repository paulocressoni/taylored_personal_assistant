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

  const play = useCallback(() => {
    const ctx = ensureCtx()
    if (!ctx) return
    if (ctx.state === 'suspended') void ctx.resume()

    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.value = 880 // A5 — a clean alarm tone
    gain.gain.value = 0.2
    osc.connect(gain).connect(ctx.destination)
    osc.start()
    osc.stop(ctx.currentTime + 1) // ring for 1 second
    oscRef.current = osc
  }, [ensureCtx])

  const stop = useCallback(() => {
    oscRef.current?.stop()
    oscRef.current = null
  }, [])

  return { play, stop }
}
