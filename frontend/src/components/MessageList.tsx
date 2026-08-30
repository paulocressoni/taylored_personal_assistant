// MessageList.tsx
// A list of messages in the chat, with support for streaming tokens and a typing indicator.
import { useEffect, useRef } from 'react'
import type { ChatMessage } from '../hooks/useChatStream'
import TypingIndicator from './TypingIndicator'

type Props = {
  messages: ChatMessage[]
  isStreaming: boolean
}

export default function MessageList({ messages, isStreaming }: Props) {
  const bottomRef = useRef<HTMLDivElement | null>(null)

  // Keep the newest message in view as tokens stream in.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isStreaming])

  // Show a placeholder when there are no messages yet.
  if (messages.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-gray-400">
        Ask me anything about your smart home…
      </div>
    )
  }

  // Render the list of messages, with user messages on the right and assistant messages on the left.
  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-4">
      {messages.map((m) => (
        <div key={m.id} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
          <div
            className={
              'max-w-[75%] whitespace-pre-wrap rounded-2xl px-4 py-2 ' +
              (m.role === 'user'
                ? 'bg-blue-600 text-white rounded-br-sm'
                : 'bg-gray-200 text-gray-900 rounded-bl-sm')
            }
          >
            {m.content}
            {/* Empty assistant bubble + still streaming → show the dots. */}
            {isStreaming && m.role === 'assistant' && m.content === '' && <TypingIndicator />}
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
