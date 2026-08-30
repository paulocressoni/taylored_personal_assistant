// TypingIndicator.tsx
// A simple animated indicator showing that the assistant is typing.
export default function TypingIndicator() {
  return (
    <span className="inline-flex gap-1" aria-label="assistant is typing">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-2 w-2 rounded-full bg-gray-400 animate-bounce"
          style={{ animationDelay: `${i * 150}ms` }}
        />
      ))}
    </span>
  )
}
