// frontend/src/hooks/useChatStream.ts
// (REPLACE the whole file — M11 Phase 3 + history.ts refactor)

import { useCallback, useEffect, useRef, useState } from 'react'
import type { components } from '../api/types'
import type { WsFrame } from '../api/ws'
import { getDeviceId } from '../lib/deviceId'
import { historyToChatMessages, type ChatMessage, type SessionHistory } from '../lib/history'
import {
  createSession,
  getActiveSessionId,
  listSessions,
  markActiveSession,
  removeSession,
  type Session,
} from '../lib/sessions'

// Re-export so components can keep importing ChatMessage from the hook
// (MessageList does). The type itself now lives in lib/history.ts so the
// pure history-mapping logic can be unit-tested in isolation.
export type { ChatMessage } from '../lib/history'

// The generated type for backend's ChatRequest pydantic model.
type ChatRequest = components['schemas']['ChatRequest']

// The backend requires a shared API key. Read once from Vite env and
// send it two ways: X-API-Key on the session HTTP calls, ?api_key= on the
// WebSocket (browsers can't set headers on a WS handshake).
function apiKey(): string {
  return import.meta.env.VITE_API_KEY ?? ''
}

// Auth header object for the session HTTP calls. Return type is annotated on
// purpose: without it, the `{}` branch of the ternary would infer as
// `{ 'X-API-Key'?: undefined }`, which fails fetch's HeadersInit check.
function authHeaders(): HeadersInit {
  const key = apiKey()
  return key ? { 'X-API-Key': key } : {}
}

function wsUrl(): string {
  // Relative to the Vite dev server, which proxies /ws/chat to the backend.
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws/chat?api_key=${encodeURIComponent(apiKey())}`
}

export function useChatStream() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [status, setStatus] = useState<string | null>(null) // "classifying intent..."
  const [error, setError] = useState<string | null>(null)
  const [lang, setLang] = useState<string | null>(null)
  const [route, setRoute] = useState<string | null>(null)

  // --- M11 Phase 1: session state --------------------------------------
  // The function initializers (() => ...) run ONCE on first render, so we
  // read the persisted values from localStorage exactly once at startup.
  const [activeSessionId, setActiveSessionId] = useState<string>(() => getActiveSessionId())
  const [sessions, setSessions] = useState<Session[]>(() => listSessions())

  // Re-read the registry from localStorage into React state after a
  // create/delete, so the sidebar stays in sync with storage.
  const refreshSessions = useCallback(() => setSessions(listSessions()), [])

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

  // Reset the on-screen chat to an empty conversation. Also closes any
  // in-flight stream so tokens from the OLD session can't write into the new
  // view. We clear the ref and flip isStreaming off HERE rather than waiting
  // for the socket's 'close' event: when the socket is still CONNECTING,
  // close() doesn't reliably fire 'close' synchronously, which would leave the
  // UI stuck streaming on a quick session switch.
  const resetChat = useCallback(() => {
    socketRef.current?.close()
    socketRef.current = null
    setIsStreaming(false)
    setMessages([])
    setError(null)
    setStatus(null)
    setLang(null)
    setRoute(null)
  }, [])

  // Load this conversation's history whenever the active session changes.
  // Runs on first mount too, so a refreshed tab repopulates the chat.
  useEffect(() => {
    // If the user switches sessions again before this request finishes,
    // `cancelled` flips to true and we discard the stale (out-of-order) result.
    let cancelled = false

    async function loadHistory() {
      try {
        const res = await fetch(`/sessions/${encodeURIComponent(activeSessionId)}/history`, {
          headers: authHeaders(),
        })
        if (!res.ok) throw new Error(`history request failed: ${res.status}`)
        const data = (await res.json()) as SessionHistory
        if (!cancelled) setMessages(data.messages.flatMap(historyToChatMessages))
      } catch {
        // Backend unreachable (or session never existed) — leave the chat
        // empty and tell the user, but don't crash.
        if (!cancelled) setError('Could not load this conversation.')
      }
    }

    loadHistory()

    // React runs this cleanup before the next effect run (or on unmount).
    // Setting `cancelled` means an in-flight response is ignored.
    return () => {
      cancelled = true
    }
  }, [activeSessionId])

  // Click a session in the sidebar -> switch to it.
  const switchSession = useCallback(
    (id: string) => {
      markActiveSession(id) // persist the choice in localStorage
      setActiveSessionId(id)
      resetChat() // show the new (empty) conversation
    },
    [resetChat],
  )

  // "+ New chat" -> brand-new conversation (fresh id => fresh server memory).
  const newSession = useCallback(() => {
    const session = createSession() // creates AND marks it active in storage
    setActiveSessionId(session.id)
    refreshSessions()
    resetChat()
  }, [refreshSessions, resetChat])

  // Delete a conversation. Also erase the checkpoint on the backend
  // via DELETE /sessions/{id} (which runs checkpointer.adelete_thread), so
  // the conversation's memory is truly gone, not just hidden from the UI.
  const deleteSession = useCallback(
    async (id: string) => {
      const wasActive = id === activeSessionId

      // 1) Erase the conversation's memory on the server (M11 checkpointer).
      // The Vite dev server proxies /sessions -> http://localhost:8000, so
      // this is a same-origin request (no CORS). fetch() only rejects on
      // NETWORK errors — an HTTP error status won't throw — so we still
      // unlink the session locally either way (deliberate fail-open).
      try {
        await fetch(`/sessions/${encodeURIComponent(id)}`, {
          method: 'DELETE',
          headers: authHeaders(),
        })
      } catch {
        console.error('failed to delete session on the backend', id)
      }

      // 2) Forget it in the browser and switch away if it was active.
      const next = removeSession(id) // returns the id that should be active now
      setActiveSessionId(next)
      refreshSessions()
      if (wasActive) resetChat() // only clear the view if we deleted the open one
    },
    [activeSessionId, refreshSessions, resetChat],
  )

  const sendMessage = useCallback(
    (text: string) => {
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
          session_id: activeSessionId, // was 'default' — now the REAL session id
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

          // The backend can send an error frame at any time. Stop streaming.
          case 'error':
            setError(frame.detail)
            setIsStreaming(false)
            socket.close()
            break

          // The backend sends a "done" frame when the reply is complete.
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
    },
    // IMPORTANT: re-create sendMessage whenever the active session changes,
    // otherwise it would capture a stale session_id (stale closure).
    [activeSessionId],
  )

  // Return everything the UI needs.
  return {
    messages,
    isStreaming,
    status,
    error,
    lang,
    route,
    sendMessage,
    sessions,
    activeSessionId,
    switchSession,
    newSession,
    deleteSession,
  }
}
