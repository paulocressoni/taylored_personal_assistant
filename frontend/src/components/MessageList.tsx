// MessageList.tsx
// A list of messages in the chat, with support for streaming tokens and a typing indicator.
// Per-token updates re-render ONLY the streaming row (memoized rows),
// and the view auto-scrolls only while the user is near the bottom.
import { memo, useEffect, useRef } from 'react'
import type { ChatMessage } from '../hooks/useChatStream'
import TypingIndicator from './TypingIndicator'

type Props = {
  messages: ChatMessage[]
  isStreaming: boolean
}

// Distance (px) from the bottom within which the user is considered "at the
// bottom" and the view keeps auto-following the stream.
const NEAR_BOTTOM_PX = 80

// One memoized message bubble. Memoization is what makes effective:
// while tokens stream, the hook updates ONLY the assistant message object,
// so every unchanged message keeps its identity and this row skips the
// re-render. Only the bubble whose content changed re-renders.
const MessageRow = memo(function MessageRow({
  message,
  isStreaming,
}: {
  message: ChatMessage
  isStreaming: boolean
}) {
  return (
    <div className={message.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
      <div
        className={
          'max-w-[75%] whitespace-pre-wrap rounded-2xl px-4 py-2 ' +
          (message.role === 'user'
            ? 'bg-blue-600 text-white rounded-br-sm'
            : 'bg-gray-200 text-gray-900 rounded-bl-sm')
        }
      >
        {message.content}
        {/* Empty assistant bubble + still streaming → show the dots. */}
        {isStreaming && message.role === 'assistant' && message.content === '' && (
          <TypingIndicator />
        )}
      </div>
    </div>
  )
})

export default function MessageList({ messages, isStreaming }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  // True while the user is following the stream (near the bottom). We only
  // auto-scroll when this is true, so scrolling up to read history is never
  // yanked back down by incoming tokens.
  const stickToBottom = useRef(true)

  // Update stickToBottom as the user scrolls: compare the scroll position
  // against the total scrollable height with a small threshold.
  const handleScroll = () => {
    const el = containerRef.current
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    stickToBottom.current = distanceFromBottom < NEAR_BOTTOM_PX
  }

  // Auto-scroll to the newest message ONLY while the user is near the bottom
  // (FE-05). Reset to "follow" whenever the conversation is cleared (e.g. a
  // session switch), so a fresh chat always tracks its first message.
  useEffect(() => {
    if (messages.length === 0) {
      stickToBottom.current = true
      return
    }
    if (stickToBottom.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
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
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="flex h-full flex-col gap-3 overflow-y-auto p-4"
    >
      {messages.map((m) => (
        <MessageRow key={m.id} message={m} isStreaming={isStreaming} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
