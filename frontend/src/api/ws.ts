/**
 * WebSocket message types for /ws/chat and /ws/voice.
 *
 * Hand-written (NOT generated) because OpenAPI does not describe WebSocket
 * traffic. Mirror the exact JSON frames the backend sends in
 * backend/app/api/routes.py chat_ws() and backend/app/voice/session.py.
 */

export type WsStatusFrame = { type: 'status'; detail: string }
export type WsTokenFrame = { type: 'token'; content: string }
export type WsErrorFrame = { type: 'error'; detail: string }
export type WsDoneFrame = {
  type: 'done'
  reply: string
  lang: string | null
  route: string | null
  complete: boolean
}

// Union of every frame the server can send. The switch in the hook narrows
// this union by `type`, which gives us compile-time safety per frame.
export type WsFrame = WsStatusFrame | WsTokenFrame | WsErrorFrame | WsDoneFrame

// --- /ws/voice -------------------------------------------------------------
//
// The voice socket carries audio AND control frames, so these unions describe
// only the JSON half: binary frames are raw mono s16le PCM and are never parsed.

// Client -> server. The start frame is validated by the backend's
// VoiceStartRequest and is the only frame that has no `type` field, because it
// runs before the session exists. The audio that follows it is binary. `stop`
// is the only control frame the session acts on; anything else is ignored.
export type VoiceStartFrame = {
  session_id: string
  device_id?: string | null
  output_sample_rate: 16000 | 24000
}
export type VoiceStopFrame = { type: 'stop' }
export type VoiceClientFrame = VoiceStartFrame | VoiceStopFrame

// Server -> client, in the order one turn produces them: `ready` once per
// socket, then `speech_start` / `transcript` / `token`+ / `audio_end` per turn.
// `error` can replace any of them, and a refused socket (1008 not configured or
// unauthenticated, 1013 busy) sends the same error frame before closing.
export type VoiceReadyFrame = {
  type: 'ready'
  input_sample_rate: number
  output_sample_rate: number
  format: string
}
export type VoiceSpeechStartFrame = { type: 'speech_start' }
export type VoiceTranscriptFrame = { type: 'transcript'; text: string }
export type VoiceTokenFrame = { type: 'token'; content: string }
export type VoiceAudioEndFrame = { type: 'audio_end' }
export type VoiceErrorFrame = { type: 'error'; detail: string }

export type VoiceServerFrame =
  | VoiceReadyFrame
  | VoiceSpeechStartFrame
  | VoiceTranscriptFrame
  | VoiceTokenFrame
  | VoiceAudioEndFrame
  | VoiceErrorFrame
