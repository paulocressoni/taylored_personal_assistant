// MessageInput.tsx
// A text input for sending messages, with a "Send" button and support for Shift+Enter to insert newlines.
import { useState } from 'react'

type Props = {
  disabled: boolean
  onSend: (text: string) => void
}

// The input is a controlled component, so the parent can clear it by changing the `key` prop.
export default function MessageInput({ disabled, onSend }: Props) {
  const [text, setText] = useState('')

  // Send the message if it's not empty and the input is not disabled.
  const submit = () => {
    if (disabled || !text.trim()) return
    onSend(text)
    setText('') // clear after sending
  }

  // Handle Enter key to send the message, and Shift+Enter to insert a newline.
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends; Shift+Enter inserts a newline.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  // Render the input and send button.
  return (
    <div className="flex gap-2 border-t bg-white p-3">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        rows={1}
        placeholder="Message the assistant…"
        disabled={disabled}
        className="flex-1 resize-none rounded-xl border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
      <button
        onClick={submit}
        disabled={disabled || !text.trim()}
        className="rounded-xl bg-blue-600 px-4 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        Send
      </button>
    </div>
  )
}
