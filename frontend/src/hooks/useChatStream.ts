import { useCallback, useEffect, useRef, useState } from 'react'
import type { components } from '../api/types'
import type { WsFrame } from '../api/ws'
import { getDeviceId } from '../lib/deviceId'

// The generated type for backend's ChatRequest pydantic model.
type ChatRequest = components['schemas']['ChatRequest']

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
}

function wsUrl(): string {
  // Relative to the Vite dev server, which proxies /ws/chat to the backend.
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws/chat`
}

export function useChatStream() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [status, setStatus] = useState<string | null>(null) // "classifying intent..."
  const [error, setError] = useState<string | null>(null)
  const [lang, setLang] = useState<string | null>(null)
  const [route, setRoute] = useState<string | null>(null)

  const socketRef = useRef<WebSocket | null>(null)

  // This effect intentionally has NO setup work. Its only job is to return a
  // cleanup that closes any live socket. StrictMode dev double-invokes this:
  // mount -> cleanup -> mount. Both the ghost cleanup and a real unmount
  // correctly close whatever socket is open.
  useEffect(() => {
    return () => {
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [])

  const sendMessage = useCallback((text: string) => {
    const trimmed = text.trim()
    if (!trimmed) return

    // One turn at a time: ignore sends while a socket is open or connecting.
    const live = socketRef.current?.readyState
    if (live === WebSocket.OPEN || live === WebSocket.CONNECTING) return

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: trimmed,
    }
    const assistantMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'assistant',
      content: '',
    }

    setMessages((prev) => [...prev, userMsg, assistantMsg])
    setIsStreaming(true)
    setError(null)
    setStatus(null)
    setLang(null)
    setRoute(null)

    const socket = new WebSocket(wsUrl())
    socketRef.current = socket

    // When the socket opens, send the user's message as a ChatRequest JSON payload.
    socket.addEventListener('open', () => {
      const payload: ChatRequest = {
        session_id: 'default', // TODO M13: real per-user sessions
        message: trimmed,
        device_id: getDeviceId(), // M17 needs this to know which device to ring
      }
      socket.send(JSON.stringify(payload))
    })

    // Handle every frame the backend sends, updating state as appropriate.
    socket.addEventListener('message', (event) => {
      let frame: WsFrame
      try {
        frame = JSON.parse(event.data) as WsFrame
      } catch {
        return // ignore anything that isn't valid JSON
      }

      switch (frame.type) {
        case 'status':
          setStatus(frame.detail)
          break

        case 'token':
          // Append this token to the assistant message we created above.
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsg.id ? { ...m, content: m.content + frame.content } : m,
            ),
          )
          break

        // The backend can send an error frame at any time, e.g. if the user's message
        // is too long or the backend is overloaded. Stop streaming and show the error.
        case 'error':
          setError(frame.detail)
          setIsStreaming(false)
          socket.close()
          break

        // The backend sends a "done" frame when the assistant's reply is complete.
        case 'done':
          // If no tokens arrived (backend fell back to the final state),
          // fill in the final reply so the bubble isn't empty.
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsg.id && m.content === '' ? { ...m, content: frame.reply } : m,
            ),
          )
          setLang(frame.lang)
          setRoute(frame.route)
          setIsStreaming(false)
          setStatus(null)
          socket.close()
          break
      }
    })

    // If the socket closes (e.g. network error), stop streaming and clear our ref.
    socket.addEventListener('close', () => {
      // Only clear OUR reference; a newer socket must not be clobbered.
      if (socketRef.current === socket) socketRef.current = null
      setIsStreaming(false)
    })

    // If the socket fails to connect, show an error and stop streaming.
    socket.addEventListener('error', () => {
      setError('Connection failed — is the backend running on :8000?')
      setIsStreaming(false)
      socket.close()
    })
  }, [])

  // Return the current state and the sendMessage function to the caller.
  return { messages, isStreaming, status, error, lang, route, sendMessage }
}
