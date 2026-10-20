import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { useAppStore } from '../store/useAppStore'
import { apiGetSession, apiListSessions, apiStreamAgent, apiStreamChat } from '../api/client'
import type { Message, AgentStep } from '../types'

export default function ChatArea() {
  const {
    currentSessionId,
    messages,
    isStreaming,
    sessions,
    agentSteps,
    setMessages,
    setSessions,
    addUserMessage,
    beginStreaming,
    appendStreamChunk,
    finalizeStream,
    setStreamError,
    patchSession,
    addAgentStep,
    clearAgentSteps,
  } = useAppStore()

  const [input, setInput] = useState('')
  const [isAgentMode, setIsAgentMode] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Load messages when session changes
  useEffect(() => {
    if (!currentSessionId) return
    apiGetSession(currentSessionId)
      .then((detail) => setMessages(detail.messages))
      .catch(console.error)
  }, [currentSessionId])

  // Auto-scroll on new messages or streaming chunks
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, agentSteps])

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [input])

  async function handleSend(content: string) {
    if (!content.trim() || isStreaming || !currentSessionId) return

    const sessionWasNew = sessions.find((s) => s.id === currentSessionId)?.title === 'New conversation'

    addUserMessage(content)
    beginStreaming()

    if (isAgentMode) {
      await apiStreamAgent(
        currentSessionId,
        content,
        (step) => addAgentStep(step),
        (done) => {
          appendStreamChunk(done.answer)
          finalizeStream(done.message_id)
          if (sessionWasNew) {
            apiListSessions()
              .then(({ sessions: updated }) => setSessions(updated))
              .catch(console.error)
          }
        },
        (err) => setStreamError(err),
      )
    } else {
      await apiStreamChat(
        currentSessionId,
        content,
        appendStreamChunk,
        (done) => {
          finalizeStream(done.message_id)
          if (sessionWasNew) {
            apiListSessions()
              .then(({ sessions: updated }) => setSessions(updated))
              .catch(console.error)
          }
        },
        (err) => setStreamError(err),
      )
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const content = input.trim()
    setInput('')
    handleSend(content)
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      const content = input.trim()
      setInput('')
      handleSend(content)
    }
  }

  // ── Empty state ────────────────────────────────────────────────────────────
  if (!currentSessionId) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center bg-surface-0 text-gray-600">
        <div className="w-12 h-12 rounded-2xl bg-surface-2 flex items-center justify-center mb-4">
          <svg className="w-6 h-6 text-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.5}
              d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
            />
          </svg>
        </div>
        <p className="text-sm font-medium text-gray-500">Select a chat or start a new one</p>
      </div>
    )
  }

  const currentSession = sessions.find((s) => s.id === currentSessionId)

  return (
    <div className="flex-1 flex flex-col bg-surface-0 min-w-0">
      {/* Header */}
      <header className="px-5 py-3 border-b border-border flex items-center gap-3 shrink-0">
        <div className="flex-1 min-w-0">
          <h1 className="text-sm font-medium text-gray-200 truncate">
            {currentSession?.title ?? 'New conversation'}
          </h1>
          <p className="text-xs text-gray-600">{currentSession?.model_default}</p>
        </div>
        {isStreaming && (
          <span className="text-xs text-accent-light animate-pulse">
            {isAgentMode ? 'Agent running…' : 'Generating…'}
          </span>
        )}
      </header>

      {/* Message list */}
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
        {messages.length === 0 && !isStreaming && (
          <div className="flex flex-col items-center justify-center h-full text-gray-600">
            <p className="text-sm">Send a message to get started</p>
          </div>
        )}

        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} agentSteps={msg.isStreaming ? agentSteps : []} />
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="px-5 py-4 border-t border-border shrink-0">
        <form onSubmit={handleSubmit} className="flex items-end gap-3">
          {/* Agent mode toggle */}
          <button
            type="button"
            onClick={() => setIsAgentMode((v) => !v)}
            disabled={isStreaming}
            title={isAgentMode ? 'Switch to Chat mode' : 'Switch to Agent mode'}
            className={`shrink-0 w-9 h-9 rounded-xl flex items-center justify-center transition-colors border ${
              isAgentMode
                ? 'bg-accent border-accent text-white'
                : 'bg-surface-2 border-border text-gray-400 hover:text-gray-200'
            } disabled:opacity-40 disabled:cursor-not-allowed`}
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17H3a2 2 0 01-2-2V5a2 2 0 012-2h14a2 2 0 012 2v10a2 2 0 01-2 2h-2"
              />
            </svg>
          </button>

          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              isAgentMode
                ? 'Ask TRINITY AGENT to do something… (Enter to send)'
                : 'Message TRINDATRON… (Enter to send, Shift+Enter for newline)'
            }
            rows={1}
            disabled={isStreaming}
            className="flex-1 bg-surface-2 border border-border rounded-xl px-4 py-3 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-accent disabled:opacity-50 resize-none transition-colors leading-relaxed"
          />
          <button
            type="submit"
            disabled={isStreaming || !input.trim()}
            className="shrink-0 w-9 h-9 rounded-xl bg-accent hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition-colors"
            title="Send"
          >
            <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"
              />
            </svg>
          </button>
        </form>
        <p className="text-xs text-gray-700 mt-2 text-center">
          {isAgentMode ? 'Agent mode — TRINDATRON can use tools.' : 'TRINDATRON can make mistakes. Verify important information.'}
        </p>
      </div>
    </div>
  )
}

// ── MessageBubble ──────────────────────────────────────────────────────────────

function MessageBubble({ message, agentSteps }: { message: Message; agentSteps: AgentStep[] }) {
  const isUser = message.role === 'user'

  if (isUser) {
    return (
      <div className="flex justify-end animate-fadeIn">
        <div className="max-w-[75%] bg-surface-2 border border-border rounded-2xl rounded-tr-sm px-4 py-3">
          <p className="text-sm text-gray-100 whitespace-pre-wrap leading-relaxed">
            {message.content}
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex gap-3 animate-fadeIn">
      <div className="w-7 h-7 rounded-lg bg-accent shrink-0 flex items-center justify-center mt-0.5">
        <span className="text-white font-bold text-xs">T</span>
      </div>
      <div className="flex-1 min-w-0 pt-0.5">
        <p className="text-xs text-accent-light font-medium mb-1.5">TRINDATRON</p>

        {/* Agent steps (shown while streaming in agent mode) */}
        {agentSteps.length > 0 && (
          <div className="mb-3 space-y-1.5">
            {agentSteps.map((step, i) => (
              <AgentStepCard key={i} step={step} />
            ))}
          </div>
        )}

        <div className="text-sm text-gray-200 whitespace-pre-wrap leading-relaxed">
          {message.content}
          {message.isStreaming && !message.content && agentSteps.length > 0 && (
            <span className="text-xs text-gray-500 italic">Working…</span>
          )}
          {message.isStreaming && (
            <span className="inline-block w-0.5 h-4 bg-accent-light ml-0.5 animate-blink align-middle" />
          )}
        </div>
      </div>
    </div>
  )
}

// ── AgentStepCard ─────────────────────────────────────────────────────────────

function AgentStepCard({ step }: { step: AgentStep }) {
  const [expanded, setExpanded] = useState(false)

  if (step.type === 'tool_call') {
    return (
      <div className="bg-surface-1 border border-border rounded-lg px-3 py-2 text-xs">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-2 w-full text-left text-gray-400 hover:text-gray-200 transition-colors"
        >
          <span className="text-accent-light font-mono">⚙</span>
          <span className="font-medium">{step.tool}</span>
          <span className="text-gray-600 ml-auto">{expanded ? '▲' : '▼'}</span>
        </button>
        {expanded && step.args && (
          <pre className="mt-2 text-gray-500 overflow-x-auto leading-relaxed">
            {JSON.stringify(step.args, null, 2)}
          </pre>
        )}
      </div>
    )
  }

  if (step.type === 'tool_result') {
    const ok = step.success !== false
    return (
      <div className={`border rounded-lg px-3 py-2 text-xs ${ok ? 'bg-surface-1 border-border' : 'bg-red-950/20 border-red-900/40'}`}>
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-2 w-full text-left hover:opacity-80 transition-opacity"
        >
          <span className={ok ? 'text-green-400' : 'text-red-400'}>{ok ? '✓' : '✗'}</span>
          <span className="text-gray-400">{step.tool}</span>
          <span className="text-gray-600 ml-auto">{expanded ? '▲' : '▼'}</span>
        </button>
        {expanded && (
          <pre className="mt-2 text-gray-500 overflow-x-auto leading-relaxed whitespace-pre-wrap">
            {step.output ?? '(no output)'}
          </pre>
        )}
      </div>
    )
  }

  return null
}
