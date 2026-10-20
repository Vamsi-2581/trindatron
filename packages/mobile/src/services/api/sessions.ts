import { parseMediaArtifact, type MediaCreation } from '@/features/media/media'
import { useLocaleStore } from '@/i18n'

import { apiRequest, openRequest } from './client'
import { streamErrorMessage } from './errors'
import { readEventStream } from './sse'
import type {
  AgentStep,
  ApprovalRequiredEvent,
  DoneEvent,
  IdempotentReplay,
  Session,
  SessionDetail,
  SessionList,
} from './types'

export function listSessions(signal?: AbortSignal): Promise<SessionList> {
  return apiRequest<SessionList>('/sessions', { signal })
}

export function createSession(input: { title?: string; projectId?: string | null; agentDefinitionId?: string | null } = {}): Promise<Session> {
  return apiRequest<Session>('/sessions', {
    method: 'POST',
    body: {
      ...(input.title ? { title: input.title } : {}),
      project_id: input.projectId ?? null,
      agent_definition_id: input.agentDefinitionId ?? null,
    },
  })
}

export function getSession(sessionId: string, signal?: AbortSignal): Promise<SessionDetail> {
  return apiRequest<SessionDetail>(`/sessions/${encodeURIComponent(sessionId)}`, { signal })
}

export function deleteSession(sessionId: string): Promise<void> {
  return apiRequest<void>(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
}

/** Deletes a message and everything after it (the Core's edit-and-resend primitive). */
export function deleteMessagesFrom(sessionId: string, messageId: string): Promise<void> {
  return apiRequest<void>(
    `/sessions/${encodeURIComponent(sessionId)}/messages/from/${encodeURIComponent(messageId)}`,
    { method: 'DELETE' },
  )
}

export function approveAgentAction(sessionId: string, actionId: string): Promise<{ status: string }> {
  return apiRequest(`/sessions/${encodeURIComponent(sessionId)}/agent/approve`, {
    method: 'POST',
    body: { action_id: actionId },
  })
}

export function rejectAgentAction(sessionId: string, actionId: string): Promise<{ status: string }> {
  return apiRequest(`/sessions/${encodeURIComponent(sessionId)}/agent/reject`, {
    method: 'POST',
    body: { action_id: actionId },
  })
}

export interface StreamHandlers {
  onStatus?: (key: string | null, message: string) => void
  onText?: (chunk: string) => void
  onAgentStep?: (step: AgentStep) => void
  onApprovalRequired?: (event: ApprovalRequiredEvent) => void
  /** A creation's progress or result (proposed Core event — see features/media/media.ts). */
  onMedia?: (media: MediaCreation) => void
}

export type StreamOutcome =
  | { kind: 'done'; event: DoneEvent }
  | { kind: 'error'; message: string; code: string | null }
  | { kind: 'replay'; sessionId: string; messageId: string }
  | { kind: 'aborted' }
  | { kind: 'interrupted' }

const STREAM_CONNECT_TIMEOUT_MS = 30_000

/**
 * Sends one turn through POST /sessions/{id}/message — the unified endpoint the
 * Web uses. The Core's Capability Router picks the pipeline (chat or agent),
 * the model and the provider; this client only renders what comes back.
 *
 * Pre-stream rejections (401, 403 plan gate, 404, 429 quota) reject with an
 * ApiError. In-stream failures resolve as { kind: 'error' }.
 */
export async function streamMessage(
  sessionId: string,
  content: string,
  handlers: StreamHandlers,
  options: { idempotencyKey: string; signal?: AbortSignal },
): Promise<StreamOutcome> {
  const { response, dispose } = await openRequest(`/sessions/${encodeURIComponent(sessionId)}/message`, {
    method: 'POST',
    // The language the app is showing — the phone's, or the one chosen in the app. The Core answers in it unless the
    // account has a language chosen (Core src/language.py); reading the message's words is only its last resort.
    body: { content, locale: useLocaleStore.getState().locale },
    headers: { Accept: 'text/event-stream', 'Idempotency-Key': options.idempotencyKey },
    signal: options.signal,
    timeoutMs: STREAM_CONNECT_TIMEOUT_MS,
  })

  try {
    if ((response.headers.get('content-type') ?? '').includes('application/json')) {
      const replay = (await response.json()) as IdempotentReplay
      return { kind: 'replay', sessionId: replay.session_id, messageId: replay.message_id }
    }

    let outcome: StreamOutcome | null = null
    const result = await readEventStream(
      response.body,
      (data) => {
        let event: Record<string, unknown>
        try {
          event = JSON.parse(data) as Record<string, unknown>
        } catch {
          return false
        }
        switch (event.type) {
          case 'status':
            handlers.onStatus?.(typeof event.key === 'string' ? event.key : null, typeof event.message === 'string' ? event.message : '')
            return false
          case 'text':
            if (typeof event.content === 'string' && event.content) handlers.onText?.(event.content)
            return false
          case 'tool_call':
          case 'tool_result':
            handlers.onAgentStep?.(event as unknown as AgentStep)
            return false
          case 'approval_required':
            handlers.onApprovalRequired?.(event as unknown as ApprovalRequiredEvent)
            return false
          case 'artifact': {
            const media = parseMediaArtifact(event.artifact)
            if (media) handlers.onMedia?.(media)
            return false
          }
          case 'done': {
            const done = event as unknown as DoneEvent
            // The agent pipeline delivers its answer on `done` instead of as text chunks.
            if (typeof done.answer === 'string' && done.answer) handlers.onText?.(done.answer)
            outcome = { kind: 'done', event: done }
            return false
          }
          case 'error': {
            const code = typeof event.error === 'string' ? event.error : null
            outcome = {
              kind: 'error',
              message: streamErrorMessage(typeof event.message === 'string' ? event.message : null, code),
              code,
            }
            return true
          }
          default:
            return false
        }
      },
      options.signal,
    )

    if (outcome) return outcome
    if (result === 'aborted') return { kind: 'aborted' }
    return { kind: 'interrupted' }
  } finally {
    dispose()
  }
}
