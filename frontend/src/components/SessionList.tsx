// frontend/src/components/SessionList.tsx
// The left sidebar: a "+ New chat" button and one row per conversation.
// "Controlled" component: it only renders props and fires callbacks — it
// never reads/writes localStorage itself (the hook does).

import type { Session } from '../lib/sessions'

type Props = {
  sessions: Session[]
  activeSessionId: string
  onSwitch: (id: string) => void // user clicked a session row
  onNew: () => void // user clicked "+ New chat"
  onDelete: (id: string) => void // user clicked the ✕ on a row
}

export default function SessionList({
  sessions,
  activeSessionId,
  onSwitch,
  onNew,
  onDelete,
}: Props) {
  return (
    <aside className="flex w-64 flex-col border-r border-gray-200 bg-white">
      {/* Top: the "new conversation" button */}
      <div className="border-b border-gray-200 p-3">
        <button
          onClick={onNew}
          className="w-full rounded-xl bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700"
        >
          + New chat
        </button>
      </div>

      {/* The scrollable list of conversations */}
      <nav className="flex-1 overflow-y-auto p-2">
        {sessions.length === 0 ? (
          <p className="px-2 py-4 text-center text-sm text-gray-400">No conversations yet</p>
        ) : (
          <ul className="space-y-1">
            {sessions.map((s) => {
              const active = s.id === activeSessionId
              return (
                <li key={s.id}>
                  {/* The row: click anywhere to switch to this session. */}
                  <div
                    onClick={() => onSwitch(s.id)}
                    className={`group flex cursor-pointer items-center justify-between rounded-lg px-3 py-2 text-sm ${
                      active
                        ? 'bg-blue-50 font-medium text-blue-700'
                        : 'text-gray-700 hover:bg-gray-100'
                    }`}
                  >
                    {/* Name, truncated if too long. */}
                    <span className="truncate">{s.name}</span>

                    {/* Delete button, revealed on hover (group-hover). */}
                    <button
                      onClick={(e) => {
                        // Without this, clicking ✕ would ALSO trigger the
                        // row's onClick above and switch to the session.
                        e.stopPropagation()
                        onDelete(s.id)
                      }}
                      className="ml-2 hidden text-gray-400 hover:text-red-600 group-hover:inline"
                      aria-label={`Delete ${s.name}`}
                    >
                      ✕
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </nav>
    </aside>
  )
}
