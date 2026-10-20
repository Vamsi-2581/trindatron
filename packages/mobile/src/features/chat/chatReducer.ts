import { pendingCreation, upsertMedia, type MediaCreation } from '@/features/media/media'
import type { AgentStep, ApprovalRequiredEvent, DoneEvent, MessageRole, SessionDetail } from '@/services/api/types'

export type ChatMessageState = 'final' | 'streaming' | 'failed' | 'stopped'

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  createdAt: string
  state: ChatMessageState
  /** Creations delivered with this answer. */
  media?: MediaCreation[]
  /** The answer ended at the plan's output limit, not because it was finished: the chat says so and offers to go on. */
  truncated?: boolean
}

const hasBody = (message: ChatMessage) => Boolean(message.content) || Boolean(message.media?.length)

export interface TurnError {
  message: string
  code: string | null
  /** The text the user sent, so a retry can resend it. */
  content: string
  retryable: boolean
}

export interface ChatState {
  phase: 'loading' | 'ready' | 'error'
  loadError: string | null
  title: string | null
  projectId: string | null
  messages: ChatMessage[]
  streaming: boolean
  activeTurn: { userId: string; assistantId: string } | null
  activity: string | null
  agentSteps: AgentStep[]
  approval: ApprovalRequiredEvent | null
  turnError: TurnError | null
  notice: string | null
}

export const initialChatState: ChatState = {
  phase: 'loading',
  loadError: null,
  title: null,
  projectId: null,
  messages: [],
  streaming: false,
  activeTurn: null,
  activity: null,
  agentSteps: [],
  approval: null,
  turnError: null,
  notice: null,
}

export type ChatAction =
  | { type: 'loaded'; detail: SessionDetail }
  | { type: 'loadFailed'; message: string }
  | { type: 'turnStarted'; content: string; turnId: string; now: string }
  | { type: 'activity'; message: string }
  | { type: 'textAppended'; chunk: string }
  | { type: 'agentStep'; step: AgentStep }
  | { type: 'media'; media: MediaCreation }
  | { type: 'approvalRequired'; event: ApprovalRequiredEvent }
  | { type: 'approvalResolved' }
  | { type: 'turnDone'; event: DoneEvent }
  | { type: 'turnFailed'; error: TurnError }
  | { type: 'turnStopped' }
  | { type: 'retrying' }
  | { type: 'dismissError' }
  | { type: 'titleUpdated'; title: string }
  | { type: 'notice'; message: string | null }

function finishTurn(state: ChatState, update: (message: ChatMessage) => ChatMessage | null): ChatMessage[] {
  const turn = state.activeTurn
  if (!turn) return state.messages
  const next: ChatMessage[] = []
  for (const message of state.messages) {
    const result = message.id === turn.userId || message.id === turn.assistantId ? update(message) : message
    if (result) next.push(result)
  }
  return next
}

const idle = { streaming: false, activeTurn: null, activity: null, approval: null } as const

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'loaded':
      return {
        ...state,
        phase: 'ready',
        loadError: null,
        title: action.detail.title,
        projectId: action.detail.project_id ?? state.projectId,
        messages: action.detail.messages
          .filter((message) => message.role !== 'system')
          .map((message) => ({
            id: message.id,
            role: message.role,
            content: message.content,
            createdAt: message.created_at,
            state: 'final',
            // A creation asked for earlier in this conversation keeps its card; the card fetches its current state.
            ...(message.media_job_id ? { media: [pendingCreation(message.media_job_id, message.created_at)] } : {}),
            ...(message.truncated ? { truncated: true } : {}),
          })),
      }

    case 'loadFailed':
      return state.messages.length > 0
        ? { ...state, phase: 'ready', notice: action.message }
        : { ...state, phase: 'error', loadError: action.message }

    case 'turnStarted': {
      const userId = `local-user-${action.turnId}`
      const assistantId = `local-assistant-${action.turnId}`
      return {
        ...state,
        streaming: true,
        activeTurn: { userId, assistantId },
        activity: null,
        agentSteps: [],
        approval: null,
        turnError: null,
        notice: null,
        messages: [
          ...state.messages,
          { id: userId, role: 'user', content: action.content, createdAt: action.now, state: 'final' },
          { id: assistantId, role: 'assistant', content: '', createdAt: action.now, state: 'streaming' },
        ],
      }
    }

    case 'activity':
      return state.streaming ? { ...state, activity: action.message } : state

    case 'textAppended': {
      const assistantId = state.activeTurn?.assistantId
      if (!assistantId) return state
      return {
        ...state,
        messages: state.messages.map((message) =>
          message.id === assistantId ? { ...message, content: message.content + action.chunk } : message,
        ),
      }
    }

    case 'agentStep':
      return state.streaming ? { ...state, agentSteps: [...state.agentSteps, action.step] } : state

    case 'media': {
      const assistantId = state.activeTurn?.assistantId
      if (!assistantId) return state
      return {
        ...state,
        messages: state.messages.map((message) =>
          message.id === assistantId ? { ...message, media: upsertMedia(message.media, action.media) } : message,
        ),
      }
    }

    case 'approvalRequired':
      return state.streaming ? { ...state, approval: action.event } : state

    case 'approvalResolved':
      return { ...state, approval: null }

    case 'turnDone': {
      const turn = state.activeTurn
      const messages = finishTurn(state, (message) => {
        if (message.id === turn?.userId) {
          return action.event.user_message_id ? { ...message, id: action.event.user_message_id } : message
        }
        // Replies TRINDATRON gives without a model (a creation started, or why not) arrive only as `answer`.
        const answered = message.content ? message : { ...message, content: action.event.answer ?? '' }
        if (!hasBody(answered)) return null
        return {
          ...answered,
          id: action.event.message_id || message.id,
          state: 'final',
          ...(action.event.truncated ? { truncated: true } : {}),
        }
      })
      return { ...state, ...idle, messages }
    }

    case 'turnFailed': {
      const messages = finishTurn(state, (message) => {
        if (message.id !== state.activeTurn?.assistantId) return message
        return hasBody(message) ? { ...message, state: 'failed' } : null
      })
      return { ...state, ...idle, messages, turnError: action.error }
    }

    case 'turnStopped': {
      const messages = finishTurn(state, (message) => {
        if (message.id !== state.activeTurn?.assistantId) return message
        return hasBody(message) ? { ...message, state: 'stopped' } : null
      })
      return { ...state, ...idle, messages }
    }

    case 'retrying':
      return { ...state, turnError: null, notice: null }

    case 'dismissError':
      return { ...state, turnError: null }

    case 'titleUpdated':
      return { ...state, title: action.title }

    case 'notice':
      return { ...state, notice: action.message }

    default:
      return state
  }
}
