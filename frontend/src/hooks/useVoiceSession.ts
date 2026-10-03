// frontend/src/hooks/useVoiceSession.ts
// Owns the voice socket end to end: microphone capture through the AudioWorklet,
// the frame protocol, and the gapless playback queue.
//
// Two things this hook deliberately does NOT do. It never decides when a turn
// starts or ends — the server owns endpointing, so the browser and the hardware
// behave identically, and this side reacts to `speech_start` instead. And it
// never resamples: capture runs at 16 kHz and playback at 24 kHz, each the native
// rate of its end of the wire.

import { useCallback, useEffect, useRef, useState } from 'react'
import type { VoiceServerFrame, VoiceStartFrame } from '../api/ws'
import { getDeviceId } from '../lib/deviceId'
import { apiKey, wsErrorMessage, wsUrl } from '../lib/socket'
import { nextPlaybackTime, pcm16ToFloat32 } from '../lib/voice'

// Capture at the rate the VAD window wants; playback at the rate the TTS
// provider emits. Both are declared to the server in the start frame, and the
// browser pays for neither conversion.
const CAPTURE_RATE = 16000
const PLAYBACK_RATE = 24000
const WORKLET_URL = '/worklets/pcm-capture.js'

/** What the microphone button shows, in one word. */
export type VoicePhase = 'idle' | 'listening' | 'thinking' | 'speaking'

type Options = {
  /** Conversation the socket belongs to: switching it hangs the socket up. */
  sessionId: string
  /** Called once a turn is fully answered, so the chat can re-read history. */
  onTurnComplete?: () => void
}

export type VoiceSession = {
  phase: VoicePhase
  /** What the server transcribed for the latest turn, once it has. */
  transcript: string | null
  /**
   * Transcript -> first audio byte for the latest turn, in ms.
   *
   * This is the part of the wait that happens AFTER the user's words are known.
   * The silence the endpointer waits for is real but not observable from here, so
   * it is not included — the browser has no way to tell speech from pause.
   */
  latencyMs: number | null
  /** Why the session stopped, when it was not the user who stopped it. */
  error: string | null
  /** Open the microphone and start a session, or hang the current one up. */
  toggle: () => void
}

export function useVoiceSession({ sessionId, onTurnComplete }: Options): VoiceSession {
  const [phase, setPhase] = useState<VoicePhase>('idle')
  const [transcript, setTranscript] = useState<string | null>(null)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const socketRef = useRef<WebSocket | null>(null)
  const micStreamRef = useRef<MediaStream | null>(null)
  const captureCtxRef = useRef<AudioContext | null>(null)
  const captureSourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const workletRef = useRef<AudioWorkletNode | null>(null)
  const playbackCtxRef = useRef<AudioContext | null>(null)
  /** Every chunk scheduled but not finished, so a flush can silence them all. */
  const sourcesRef = useRef<AudioBufferSourceNode[]>([])
  /** Where the next chunk should start, on the playback context's clock. */
  const cursorRef = useRef(0)
  /** Per-turn clocks for the latency readout. */
  const transcriptAtRef = useRef<number | null>(null)
  const firstAudioAtRef = useRef<number | null>(null)
  /**
   * Set when the server says the turn's audio is complete. The phase only returns
   * to "listening" once the QUEUE has drained: chunks are scheduled ahead of
   * realtime, so the server finishing is not the same as the audio finishing.
   */
  const serverDoneRef = useRef(false)
  /**
   * Generation counter. start() awaits the microphone permission prompt, which can
   * outlive a stop() — every await re-checks that its own generation is still the
   * current one, so a superseded start can never wire up a live session.
   */
  const generationRef = useRef(0)

  // Held in a ref so the socket handlers always call the LATEST callback without
  // the effect that owns them depending on it.
  const onTurnCompleteRef = useRef(onTurnComplete)
  useEffect(() => {
    onTurnCompleteRef.current = onTurnComplete
  }, [onTurnComplete])

  // Release everything a session acquired. Idempotent, because the socket's own
  // close event and stop() both call it, and StrictMode's ghost unmount calls it
  // on a hook instance where nothing was ever started.
  const teardown = useCallback(() => {
    // Stopping the tracks is what turns the browser's recording indicator off.
    micStreamRef.current?.getTracks().forEach((track) => track.stop())
    micStreamRef.current = null

    if (workletRef.current) workletRef.current.port.onmessage = null
    workletRef.current?.disconnect()
    workletRef.current = null
    captureSourceRef.current?.disconnect()
    captureSourceRef.current = null
    if (captureCtxRef.current && captureCtxRef.current.state !== 'closed') {
      void captureCtxRef.current.close()
    }
    captureCtxRef.current = null

    for (const source of sourcesRef.current) {
      try {
        source.stop()
      } catch {
        // Already finished: stop() throws on a source that has ended.
      }
    }
    sourcesRef.current = []
    cursorRef.current = 0
    serverDoneRef.current = false
    if (playbackCtxRef.current && playbackCtxRef.current.state !== 'closed') {
      void playbackCtxRef.current.close()
    }
    playbackCtxRef.current = null

    setPhase('idle')
  }, [])

  const stop = useCallback(() => {
    generationRef.current += 1 // a start still awaiting permission is now stale
    const socket = socketRef.current
    socketRef.current = null
    socket?.close()
    teardown()
  }, [teardown])

  const start = useCallback(async () => {
    if (socketRef.current) return
    const generation = generationRef.current + 1
    generationRef.current = generation

    setError(null)
    setTranscript(null)
    setLatencyMs(null)
    transcriptAtRef.current = null
    firstAudioAtRef.current = null
    serverDoneRef.current = false

    // The microphone prompt only appears on a user gesture, which is why start()
    // is always reached from the button's click.
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          // Ask for the wire rate here and the browser resamples in one place,
          // instead of us doing a second conversion on every frame.
          sampleRate: CAPTURE_RATE,
        },
      })
    } catch {
      setError('Microphone access was refused.')
      return
    }
    if (generationRef.current !== generation) {
      // stop() ran while the prompt was open: give the device straight back.
      stream.getTracks().forEach((track) => track.stop())
      return
    }
    micStreamRef.current = stream

    const captureCtx = new AudioContext({ sampleRate: CAPTURE_RATE })
    const playbackCtx = new AudioContext({ sampleRate: PLAYBACK_RATE })
    captureCtxRef.current = captureCtx
    playbackCtxRef.current = playbackCtx
    // A context created outside a gesture starts suspended; the click that
    // reached start() is a gesture, so resuming here is enough.
    await captureCtx.resume()
    await playbackCtx.resume()
    // Loaded now rather than on `ready`, so the first frames are not delayed by a
    // module fetch. addModule caches by URL, so repeating it is cheap.
    try {
      await captureCtx.audioWorklet.addModule(WORKLET_URL)
    } catch {
      setError('Could not load the capture worklet.')
      stop()
      return
    }
    if (generationRef.current !== generation) {
      stop()
      return
    }

    /** Stop everything playing — barge-in, and the way audio ends on stop(). */
    const flush = () => {
      for (const source of sourcesRef.current) {
        try {
          source.stop()
        } catch {
          // Already ended.
        }
      }
      sourcesRef.current = []
      cursorRef.current = 0
    }

    /** Schedule one chunk of PCM so it plays immediately after the previous one. */
    const enqueue = (chunk: ArrayBuffer) => {
      const context = playbackCtxRef.current
      if (!context) return
      const samples = pcm16ToFloat32(new Uint8Array(chunk))
      if (samples.length === 0) return

      const buffer = context.createBuffer(1, samples.length, PLAYBACK_RATE)
      // getChannelData().set() rather than copyToChannel(): the latter demands a
      // Float32Array<ArrayBuffer>, and a plain Float32Array is typed as possibly
      // backed by a SharedArrayBuffer, which it refuses.
      buffer.getChannelData(0).set(samples)
      const source = context.createBufferSource()
      source.buffer = buffer
      source.connect(context.destination)
      const startAt = nextPlaybackTime(context.currentTime, cursorRef.current)
      source.start(startAt)
      cursorRef.current = startAt + buffer.duration
      sourcesRef.current = [...sourcesRef.current, source]
      source.onended = () => {
        sourcesRef.current = sourcesRef.current.filter((item) => item !== source)
        if (sourcesRef.current.length === 0 && serverDoneRef.current) {
          serverDoneRef.current = false
          setPhase('listening')
        }
      }

      if (firstAudioAtRef.current === null) {
        firstAudioAtRef.current = performance.now()
        if (transcriptAtRef.current !== null) {
          setLatencyMs(firstAudioAtRef.current - transcriptAtRef.current)
        }
      }
      // Same-value updates bail out of the re-render, so one chunk per frame does
      // not mean one render per frame.
      setPhase((current) => (current === 'speaking' ? current : 'speaking'))
    }

    // Per-connection flag: an error frame already explained the failure, so the
    // close that follows must not replace a specific detail with a generic one.
    let resolved = false

    const key = apiKey()
    const socket = key
      ? new WebSocket(wsUrl('/ws/voice'), [key])
      : new WebSocket(wsUrl('/ws/voice'))
    // Binary frames arrive as Blob unless asked otherwise, and the playback path
    // wants the bytes synchronously — so ask once, here.
    socket.binaryType = 'arraybuffer'
    socketRef.current = socket

    // The start frame must be the FIRST thing on the wire: the route reads the
    // next message to validate the session before any audio is accepted.
    socket.addEventListener('open', () => {
      const startFrame: VoiceStartFrame = {
        session_id: sessionId,
        device_id: getDeviceId(),
        output_sample_rate: PLAYBACK_RATE,
      }
      socket.send(JSON.stringify(startFrame))
    })

    socket.addEventListener('message', (event) => {
      // Audio is binary, not JSON, and the parse below would happily swallow it.
      // Dispatch it first so a dropped reply is impossible.
      if (event.data instanceof ArrayBuffer) {
        enqueue(event.data)
        return
      }
      let frame: VoiceServerFrame
      try {
        frame = JSON.parse(event.data as string) as VoiceServerFrame
      } catch {
        return
      }
      switch (frame.type) {
        case 'ready': {
          // Only now is the server willing to read audio. Wiring capture earlier
          // would send a binary frame that it reads as the start frame, and the
          // socket would be refused with 1003.
          const source = captureCtx.createMediaStreamSource(stream)
          const worklet = new AudioWorkletNode(captureCtx, 'pcm-capture')
          source.connect(worklet)
          // The worklet never writes to its output, so this is silence — but a
          // node is only pulled (process() only runs) while something pulls it,
          // and the destination is what does the pulling. Without this line no
          // frames are ever produced.
          worklet.connect(captureCtx.destination)
          worklet.port.onmessage = (message: MessageEvent<ArrayBuffer>) => {
            if (socket.readyState === WebSocket.OPEN) socket.send(message.data)
          }
          captureSourceRef.current = source
          workletRef.current = worklet
          setPhase('listening')
          break
        }

        case 'speech_start':
          // The server heard the user start talking: the answer in flight is
          // stale, so stop playing it and go back to listening for the turn.
          flush()
          firstAudioAtRef.current = null
          setPhase('thinking')
          break

        case 'transcript':
          setTranscript(frame.text)
          transcriptAtRef.current = performance.now()
          break

        case 'token':
          // The spoken answer arrives as audio; the tokens are only useful if the
          // transcript is rendered as text alongside it.
          break

        case 'audio_end':
          serverDoneRef.current = true
          if (sourcesRef.current.length === 0) setPhase('listening')
          onTurnCompleteRef.current?.()
          break

        case 'error':
          resolved = true
          setError(frame.detail)
          setPhase('listening')
          break
      }
    })

    socket.addEventListener('close', (event: CloseEvent) => {
      if (socketRef.current === socket) socketRef.current = null
      const intentional = generationRef.current !== generation
      teardown()
      if (intentional || resolved) return
      const message = wsErrorMessage(event.code)
      if (message) setError(message)
    })

    socket.addEventListener('error', () => {
      // 'close' always follows and carries the code, so only the pre-handshake
      // failure needs naming here.
      if (!resolved) setError('Connection failed — is the backend running?')
    })
  }, [sessionId, stop, teardown])

  const toggle = useCallback(() => {
    if (socketRef.current) stop()
    else void start()
  }, [start, stop])

  // One cleanup for two jobs: StrictMode's ghost unmount (a no-op, nothing is
  // started) and a real unmount or session switch, which must hang up — a socket
  // belongs to one conversation, so leaving it open would answer the next
  // utterance into the previous thread.
  useEffect(() => {
    return () => stop()
  }, [sessionId, stop])

  return { phase, transcript, latencyMs, error, toggle }
}
