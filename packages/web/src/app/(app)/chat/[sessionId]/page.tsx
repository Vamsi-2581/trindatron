'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { useParams, useSearchParams } from 'next/navigation'
import { AlertTriangle, CheckCircle, MousePointerClick, X } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'
import {
  apiGetSession, apiSendMessage, apiStreamAgent, apiApproveAgentAction, apiRejectAgentAction, apiDeleteMessagesFrom,
  apiGetCapabilities, apiGetPreferences,
} from '@/api/client'
import { MessageBubble } from '@/components/chat/message-bubble'
import { ActivityStream } from '@/components/chat/activity-stream'
import { Composer } from '@/components/chat/composer'

export default function ChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  // Computer Use opens a conversation in agent mode: every turn goes to the agent, and its approval gate
  // (click, fill, write, run) pauses the work until the person decides — exactly as in any agent turn.
  const agentMode = useSearchParams()?.get('mode') === 'agent'
  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const isAtBottomRef = useRef(true)
  const [sessionLoaded, setSessionLoaded] = useState(false)
  const [approvalLoading, setApprovalLoading] = useState(false)
  // Settings → Voz: read each finished answer aloud, only when voice output is available to this account.
  const autoReadEnabledRef = useRef(false)
  const [autoReadId, setAutoReadId] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([apiGetPreferences(), apiGetCapabilities()])
      .then(([preferences, capabilities]) => {
        autoReadEnabledRef.current = preferences.voice_auto_play && capabilities.audio_output?.available === true
      })
      .catch(() => { autoReadEnabledRef.current = false })
  }, [])

  const {
    messages, isStreaming, statusMessages, agentSteps, pendingApproval,
    pendingMessage, setPendingMessage, setPendingApproval,
    setMessages, selectSession, patchSession,
    addUserMessage, beginStreaming, appendStreamChunk, finalizeStream, setStreamError,
    addStatusMessage, addAgentStep, attachMediaToStreaming, attachJobToStreaming,
  } = useAppStore()

  // Load session on mount / session change. `cancelled` guards against a
  // stale response clobbering live state — under React Strict Mode (dev)
  // this effect double-invokes, firing two overlapping GETs; without this
  // guard the second (redundant) one can resolve after sendMessage() has
  // already populated messages locally, wiping them back to the empty
  // snapshot fetched before the turn started.
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    setSessionLoaded(false)
    selectSession(sessionId)
    apiGetSession(sessionId)
      .then((data) => {
        if (cancelled) return
        setMessages(data.messages)
        setSessionLoaded(true)
      })
      .catch(() => {
        if (!cancelled) setSessionLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [sessionId, selectSession, setMessages])

  const sendMessage = useCallback(async (content: string) => {
    if (isStreaming || !sessionId) return

    isAtBottomRef.current = true  // always scroll for the reply to a message the user just sent
    addUserMessage(content)
    beginStreaming()
    let answerId: string | null = null

    if (agentMode) {
      await apiStreamAgent(
        sessionId,
        content,
        (step) => addAgentStep(step),
        (done) => {
          if (done.answer) appendStreamChunk(done.answer)
          finalizeStream(done.message_id, done.user_message_id)
          answerId = done.message_id
        },
        (errMsg) => setStreamError(errMsg),
        (statusMsg) => addStatusMessage(statusMsg),
        (event) => setPendingApproval(event as Parameters<typeof setPendingApproval>[0]),
      )
    } else {
      await apiSendMessage(
        sessionId,
        content,
        (chunk) => appendStreamChunk(chunk),
        (done) => {
          // A presentation (or any job) this turn started: its card follows it until the file is ready.
          if (done.job_id) attachJobToStreaming(done.job_id)
          finalizeStream(done.message_id, done.user_message_id)
          answerId = done.message_id
        },
        (errMsg) => setStreamError(errMsg),
        (statusMsg) => addStatusMessage(statusMsg),
        (step) => addAgentStep(step),
        (event) => setPendingApproval(event),
        (artifact) => attachMediaToStreaming(artifact.id),
      )
    }

    if (answerId && autoReadEnabledRef.current) setAutoReadId(answerId)

    // After each turn, refresh the session title from the backend.
    // The backend auto-generates a title from the first user message.
    // This keeps the sidebar in sync without requiring a full page reload.
    apiGetSession(sessionId)
      .then((s) => patchSession(sessionId, { title: s.title }))
      .catch(() => null)
  }, [
    sessionId, isStreaming, agentMode,
    addUserMessage, beginStreaming, appendStreamChunk,
    finalizeStream, setStreamError, addStatusMessage, addAgentStep, setPendingApproval, patchSession,
    attachMediaToStreaming, attachJobToStreaming,
  ])

  async function handleApprove() {
    if (!sessionId || !pendingApproval || approvalLoading) return
    setApprovalLoading(true)
    try {
      await apiApproveAgentAction(sessionId, pendingApproval.action_id)
      setPendingApproval(null)
    } catch (err) {
      setStreamError(err instanceof Error ? err.message : 'Falha ao aprovar ação')
    } finally {
      setApprovalLoading(false)
    }
  }

  async function handleReject() {
    if (!sessionId || !pendingApproval || approvalLoading) return
    setApprovalLoading(true)
    try {
      await apiRejectAgentAction(sessionId, pendingApproval.action_id)
      setPendingApproval(null)
    } catch (err) {
      setStreamError(err instanceof Error ? err.message : 'Falha ao rejeitar ação')
    } finally {
      setApprovalLoading(false)
    }
  }

  const editMessage = useCallback(async (messageId: string, messageIndex: number, newContent: string) => {
    if (isStreaming || !sessionId) return
    try {
      await apiDeleteMessagesFrom(sessionId, messageId)
    } catch {
      // If delete fails (e.g. temp ID not yet in DB), proceed anyway — backend history may have stale entries
    }
    setMessages(messages.slice(0, messageIndex))
    await sendMessage(newContent)
  }, [sessionId, isStreaming, messages, setMessages, sendMessage])

  // Consume pending message set by home page when creating a new session
  useEffect(() => {
    if (!sessionLoaded || !pendingMessage || isStreaming) return
    const msg = pendingMessage
    setPendingMessage(null)
    sendMessage(msg)
  }, [sessionLoaded, pendingMessage, isStreaming, sendMessage, setPendingMessage])

  // Track whether the user is at (or near) the bottom of the scroll container.
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    function onScroll() {
      const el = scrollContainerRef.current
      if (!el) return
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      isAtBottomRef.current = distanceFromBottom < 100
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  // Auto-scroll only when the user is at the bottom.
  useEffect(() => {
    if (isAtBottomRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages.length, statusMessages.length])

  return (
    <div className="flex flex-col h-full">
      {agentMode && (
        <div className="border-b border-border bg-surface-1 shrink-0">
          <div className="max-w-3xl mx-auto px-6 py-2 flex items-center gap-2 text-xs text-text-muted">
            <MousePointerClick className="w-3.5 h-3.5 text-accent-light" />
            <span>Computer Use — ações que alteram algo pedem sua aprovação antes de acontecer.</span>
          </div>
        </div>
      )}
      {/* Messages area */}
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">
          {messages.map((msg, index) => {
            const isStreamingMsg = msg.isStreaming

            // While streaming with no content yet and status messages present,
            // show the activity stream instead of an empty bubble.
            if (isStreamingMsg && !msg.content && statusMessages.length > 0 && agentSteps.length === 0) {
              return (
                <ActivityStream
                  key={msg.id}
                  messages={statusMessages}
                  isActive={true}
                />
              )
            }

            return (
              <MessageBubble
                key={msg.id}
                message={msg}
                agentSteps={isStreamingMsg ? agentSteps : []}
                statusMessages={isStreamingMsg ? statusMessages : []}
                autoRead={msg.id === autoReadId}
                onEdit={msg.role === 'user' && !isStreaming
                  ? (newContent) => editMessage(msg.id, index, newContent)
                  : undefined}
              />
            )
          })}

          <div ref={bottomRef} />
        </div>
      </div>

      {/* Approval gate — pauses the agent's ReAct loop for a CONFIRM-level
          tool call (write_file/execute_shell) until the user resolves it. */}
      {pendingApproval && (
        <div className="border-t border-yellow-500/40 bg-yellow-500/10 shrink-0">
          <div className="max-w-3xl mx-auto px-6 py-3 flex items-center gap-3">
            <AlertTriangle className="w-4 h-4 text-yellow-400 shrink-0" />
            <p className="flex-1 text-sm text-text-secondary">{pendingApproval.description}</p>
            <button
              type="button"
              onClick={() => void handleApprove()}
              disabled={approvalLoading}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-green-600 hover:bg-green-500 text-white text-xs font-medium transition-colors disabled:opacity-50"
            >
              <CheckCircle className="w-3.5 h-3.5" />
              Aprovar
            </button>
            <button
              type="button"
              onClick={() => void handleReject()}
              disabled={approvalLoading}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface-2 hover:bg-surface-3 text-text-secondary text-xs font-medium transition-colors disabled:opacity-50"
            >
              <X className="w-3.5 h-3.5" />
              Rejeitar
            </button>
          </div>
        </div>
      )}

      {/* Composer area */}
      <div className="border-t border-border bg-surface-1 shrink-0">
        <div className="max-w-3xl mx-auto px-6 pt-3 pb-4">
          <Composer
            onSend={sendMessage}
            disabled={isStreaming}
            placeholder={agentMode ? 'Diga o que você quer que o TRINDATRON faça…' : 'Escreva o que precisa fazer…'}
            sessionId={sessionId}
          />
        </div>
      </div>
    </div>
  )
}
