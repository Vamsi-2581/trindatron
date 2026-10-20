import { useEffect } from 'react'
import { useAppStore } from '../store/useAppStore'
import { apiListSessions, apiCreateSession, apiDeleteSession, setToken } from '../api/client'

export default function Sidebar() {
  const {
    user,
    sessions,
    currentSessionId,
    isStreaming,
    setSessions,
    prependSession,
    removeSession,
    selectSession,
    setMessages,
    clearAuth,
  } = useAppStore()

  useEffect(() => {
    apiListSessions()
      .then(({ sessions }) => setSessions(sessions))
      .catch(console.error)
  }, [])

  async function handleNewChat() {
    if (isStreaming) return
    try {
      const session = await apiCreateSession()
      prependSession(session)
      selectSession(session.id)
    } catch (err) {
      console.error('Failed to create session', err)
    }
  }

  async function handleDelete(e: React.MouseEvent, id: string) {
    e.stopPropagation()
    if (isStreaming && currentSessionId === id) return
    try {
      await apiDeleteSession(id)
      removeSession(id)
      if (currentSessionId === id) setMessages([])
    } catch (err) {
      console.error('Failed to delete session', err)
    }
  }

  function handleLogout() {
    setToken(null)
    clearAuth()
  }

  return (
    <aside className="w-64 shrink-0 flex flex-col bg-surface-1 border-r border-border">
      {/* Header */}
      <div className="p-3 border-b border-border">
        <div className="flex items-center gap-2 mb-3 px-1">
          <div className="w-6 h-6 rounded bg-accent flex items-center justify-center shrink-0">
            <span className="text-white font-bold text-xs">T</span>
          </div>
          <span className="text-white font-semibold text-sm tracking-tight">TRINDATRON</span>
        </div>
        <button
          onClick={handleNewChat}
          disabled={isStreaming}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-surface-2 hover:bg-surface-3 disabled:opacity-50 disabled:cursor-not-allowed text-gray-200 text-sm font-medium transition-colors border border-border"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          New Chat
        </button>
      </div>

      {/* Session list */}
      <nav className="flex-1 overflow-y-auto py-2 px-2 space-y-0.5">
        {sessions.length === 0 && (
          <p className="text-gray-600 text-xs text-center py-6 px-3">
            No conversations yet.
            <br />
            Start a new chat above.
          </p>
        )}
        {sessions.map((sess) => (
          <button
            key={sess.id}
            onClick={() => selectSession(sess.id)}
            className={`group w-full flex items-center gap-2 px-3 py-2 rounded-lg text-left text-sm transition-colors ${
              sess.id === currentSessionId
                ? 'bg-surface-3 text-white'
                : 'text-gray-400 hover:bg-surface-2 hover:text-gray-200'
            }`}
          >
            <svg
              className="w-3.5 h-3.5 shrink-0 text-gray-600"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
              />
            </svg>
            <span className="truncate flex-1">{sess.title}</span>
            <span
              role="button"
              onClick={(e) => handleDelete(e, sess.id)}
              className="opacity-0 group-hover:opacity-100 p-0.5 rounded hover:text-red-400 transition-opacity shrink-0"
              title="Delete"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </span>
          </button>
        ))}
      </nav>

      {/* User footer */}
      <div className="p-3 border-t border-border">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-full bg-accent-dim flex items-center justify-center shrink-0">
            <span className="text-accent-light font-medium text-xs uppercase">
              {user?.email?.[0] ?? '?'}
            </span>
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-gray-300 text-xs truncate">{user?.email}</p>
            <p className="text-gray-600 text-xs capitalize">{user?.plan} plan</p>
          </div>
          <button
            onClick={handleLogout}
            title="Sign out"
            className="p-1 text-gray-600 hover:text-gray-300 transition-colors shrink-0"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"
              />
            </svg>
          </button>
        </div>
      </div>
    </aside>
  )
}
