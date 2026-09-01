// frontend/src/App.tsx
// Main app: session sidebar on the left, chat column on the right.
// (REPLACE the whole file — M11 Phase 1 adds the session sidebar)

import { useChatStream } from './hooks/useChatStream'
import MessageList from './components/MessageList'
import MessageInput from './components/MessageInput'
import LanguageBadge from './components/LanguageBadge'
import SessionList from './components/SessionList'

export default function App() {
  const {
    messages,
    isStreaming,
    status,
    error,
    lang,
    sendMessage,
    sessions,
    activeSessionId,
    switchSession,
    newSession,
    deleteSession,
  } = useChatStream()

  return (
    // h-screen = full viewport height; flex row puts the sidebar on the left.
    <div className="flex h-screen bg-gray-50">
      {/* Left: session sidebar */}
      <SessionList
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSwitch={switchSession}
        onNew={newSession}
        onDelete={deleteSession}
      />

      {/* Right: the chat column */}
      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b bg-white px-4 py-3">
          <h1 className="text-lg font-semibold">Taylored Assistant</h1>
          <div className="flex items-center gap-3">
            <LanguageBadge lang={lang} />
            {isStreaming && status && <span className="text-xs text-gray-500">{status}</span>}
          </div>
        </header>

        {error && <div className="bg-red-100 px-4 py-2 text-sm text-red-700">{error}</div>}

        <main className="flex-1 overflow-hidden">
          <MessageList messages={messages} isStreaming={isStreaming} />
        </main>

        <MessageInput disabled={isStreaming} onSend={sendMessage} />
      </div>
    </div>
  )
}
