// frontend/src/App.tsx
// The main app component, which renders the chat interface and handles sending messages.
import { useChatStream } from './hooks/useChatStream'
import MessageList from './components/MessageList'
import MessageInput from './components/MessageInput'
import LanguageBadge from './components/LanguageBadge'

// The main app component, which renders the chat interface and handles sending messages.
export default function App() {
  const { messages, isStreaming, status, error, lang, sendMessage } = useChatStream()

  // Render the chat interface, including the message list, input, and status/error messages.
  return (
    <div className="flex h-screen flex-col bg-gray-50">
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
  )
}
