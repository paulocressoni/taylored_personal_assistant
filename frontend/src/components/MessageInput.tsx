// MessageInput.tsx
// A text input for sending messages, with a "Send" button and support for Shift+Enter to insert newlines.
import { useEffect, useRef, useState } from 'react'

type Props = {
  disabled: boolean
  onSend: (text: string) => void
}

// The input is a controlled component, so the parent can clear it by changing the `key` prop.
export default function MessageInput({ disabled, onSend }: Props) {
  const [text, setText] = useState('')

  // A ref gives us a direct handle to the DOM <textarea> element. React state
  // re-renders; a ref lets us touch the actual element (focus, scroll, size)
  // without causing a re-render.
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Return focus to the textarea whenever it becomes enabled again.
  // Why an effect and not just focus() inside submit(): after you send, the
  // parent sets disabled={isStreaming}, so the textarea is DISABLED while the
  // assistant replies — and a disabled element can't be focused. The input
  // only becomes focusable when streaming ends, so we react to that moment
  // here. It also focuses on first mount (handy UX).
  useEffect(() => {
    if (!disabled) textareaRef.current?.focus()
  }, [disabled])

  // Send the message if it's not empty and the input is not disabled.
  const submit = () => {
    if (disabled || !text.trim()) return
    onSend(text)
    setText('') // clear after sending
    // Best-effort immediate refocus (covers cases where onSend is synchronous
    // and the input isn't disabled). During streaming this is a no-op; the
    // effect above restores focus once streaming finishes.
    textareaRef.current?.focus()
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
        ref={textareaRef}
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
