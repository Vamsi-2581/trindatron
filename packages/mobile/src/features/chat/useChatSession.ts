import { useCallback, useEffect, useReducer, useRef } from 'react'

import { hasTranslation, t } from '@/i18n'
import { ApiError, toApiError, userMessage, type ApiErrorKind } from '@/services/api/errors'
import { safeUserMessage } from '@/services/api/redact'
import {
  approveAgentAction,
  deleteMessagesFrom,
  getSession,
  rejectAgentAction,
  streamMessage,
} from '@/services/api/sessions'
import { createId } from '@/utils/ids'

import { chatReducer, initialChatState, type ChatState, type TurnError } from './chatReducer'

const RETRYABLE_KINDS: ReadonlySet<ApiErrorKind> = new Set([
  'network',
  'timeout',
  'server',
  'dependency_unavailable',
  'rate_limited',
  'conflict',
])

/** Stream chunks are coalesced so a long answer re-renders ~20×/s, not once per token. */
const TEXT_FLUSH_MS = 50

export function turnErrorFrom(error: ApiError, content: string): TurnError {
  return {
    message: error.message,
    code: error.code ?? error.kind,
    content,
    retryable: RETRYABLE_KINDS.has(error.kind),
  }
}

export interface ChatSession {
  state: ChatState
  send: (content: string) => Promise<void>
  cancel: () => void
  retry: () => Promise<void>
  resolveApproval: (approved: boolean) => Promise<void>
  dismissError: () => void
  reload: () => Promise<void>
}

export function useChatSession(sessionId: string): ChatSession {
  const [state, dispatch] = useReducer(chatReducer, initialChatState)
  const stateRef = useRef(state)
  const controllerRef = useRef<AbortController | null>(null)
  const textBuffer = useRef('')
  const flushTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    stateRef.current = state
  }, [state])

  const flush = useCallback(() => {
    if (flushTimer.current) {
      clearTimeout(flushTimer.current)
      flushTimer.current = null
    }
    if (textBuffer.current) {
      dispatch({ type: 'textAppended', chunk: textBuffer.current })
      textBuffer.current = ''
    }
  }, [])

  const queueText = useCallback(
    (chunk: string) => {
      textBuffer.current += chunk
      if (!flushTimer.current) flushTimer.current = setTimeout(flush, TEXT_FLUSH_MS)
    },
    [flush],
  )

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        dispatch({ type: 'loaded', detail: await getSession(sessionId, signal) })
      } catch (error) {
        if (signal?.aborted) return
        dispatch({ type: 'loadFailed', message: userMessage(error) })
      }
    },
    [sessionId],
  )

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => {
      controller.abort()
      controllerRef.current?.abort()
      if (flushTimer.current) clearTimeout(flushTimer.current)
    }
  }, [load])

  const send = useCallback(
    async (raw: string) => {
      const content = raw.trim()
      if (!content || stateRef.current.streaming) return

      const controller = new AbortController()
      controllerRef.current = controller
      // Mark streaming synchronously so a double tap cannot start a second turn.
      stateRef.current = { ...stateRef.current, streaming: true }
      dispatch({ type: 'turnStarted', content, turnId: createId(), now: new Date().toISOString() })

      try {
        const outcome = await streamMessage(
          sessionId,
          content,
          {
            onStatus: (key, message) => {
              // Same activity keys as the Web; the Core's fallback text is used only if it names nothing internal.
              dispatch({
                type: 'activity',
                message: key && hasTranslation(key) ? t(key) : safeUserMessage(message, t('chat.thinking')),
              })
            },
            onText: queueText,
            onAgentStep: (step) => dispatch({ type: 'agentStep', step }),
            onApprovalRequired: (event) => dispatch({ type: 'approvalRequired', event }),
            onMedia: (media) => dispatch({ type: 'media', media }),
          },
          { idempotencyKey: createId(), signal: controller.signal },
        )
        flush()

        switch (outcome.kind) {
          case 'done':
            dispatch({ type: 'turnDone', event: outcome.event })
            getSession(sessionId)
              .then((detail) => dispatch({ type: 'titleUpdated', title: detail.title }))
              .catch(() => undefined)
            break
          case 'error':
            dispatch({
              type: 'turnFailed',
              error: {
                message: outcome.message,
                code: outcome.code,
                content,
                retryable: outcome.code !== 'quality_not_entitled',
              },
            })
            break
          case 'interrupted':
            dispatch({
              type: 'turnFailed',
              error: { message: t('chat.interrupted'), code: 'interrupted', content, retryable: true },
            })
            break
          case 'aborted':
            dispatch({ type: 'turnStopped' })
            break
          case 'replay':
            await load()
            break
        }
      } catch (error) {
        flush()
        const apiError = toApiError(error)
        if (apiError.kind === 'aborted') dispatch({ type: 'turnStopped' })
        else dispatch({ type: 'turnFailed', error: turnErrorFrom(apiError, content) })
      } finally {
        if (controllerRef.current === controller) controllerRef.current = null
      }
    },
    [sessionId, queueText, flush, load],
  )

  const cancel = useCallback(() => {
    controllerRef.current?.abort()
  }, [])

  /**
   * The Core stores the user's message before generating, so a failed turn
   * leaves it in the history. Resending blindly would duplicate it: reconcile
   * with the server first and delete that orphan, then send again.
   */
  const retry = useCallback(async () => {
    const failed = stateRef.current.turnError
    if (!failed || stateRef.current.streaming) return
    dispatch({ type: 'retrying' })
    try {
      const detail = await getSession(sessionId)
      const last = detail.messages[detail.messages.length - 1]
      if (last && last.role === 'user' && last.content.trim() === failed.content) {
        await deleteMessagesFrom(sessionId, last.id)
        dispatch({ type: 'loaded', detail: { ...detail, messages: detail.messages.slice(0, -1) } })
      } else {
        dispatch({ type: 'loaded', detail })
      }
    } catch (error) {
      dispatch({ type: 'turnFailed', error: turnErrorFrom(toApiError(error), failed.content) })
      return
    }
    await send(failed.content)
  }, [sessionId, send])

  const resolveApproval = useCallback(
    async (approved: boolean) => {
      const approval = stateRef.current.approval
      if (!approval) return
      try {
        if (approved) await approveAgentAction(sessionId, approval.action_id)
        else await rejectAgentAction(sessionId, approval.action_id)
        dispatch({ type: 'approvalResolved' })
      } catch (error) {
        dispatch({ type: 'notice', message: userMessage(error) })
      }
    },
    [sessionId],
  )

  const dismissError = useCallback(() => dispatch({ type: 'dismissError' }), [])
  const reload = useCallback(() => load(), [load])

  return { state, send, cancel, retry, resolveApproval, dismissError, reload }
}
