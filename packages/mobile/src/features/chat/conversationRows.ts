import type { IMessage } from '@kesha-antonov/react-native-chat'

import type { AgentStep } from '@/services/api/types'

import type { ChatMessage, ChatState } from './chatReducer'

/** The two sides of a conversation, as the chat list tells them apart. */
export const PERSON = { _id: 'person' } as const
export const TRINDATRON = { _id: 'trindatron', name: 'TRINDATRON' } as const

/**
 * One row of the conversation list: the shape the chat library lays out, with our message carried inside unchanged.
 * The list only positions rows; every row is still drawn by our MessageBubble from `message`.
 */
export interface ConversationRow extends IMessage {
  _id: string
  message: ChatMessage
  /** What TRINDATRON is doing right now. Only the reply in progress carries it. */
  activity: string | null
  /** The agent's steps so far. Only the reply in progress carries them. */
  agentSteps: AgentStep[]
  /** The newest message: the only one an answer can be continued from. */
  latest: boolean
}

const NO_STEPS: AgentStep[] = []

/**
 * Newest first, because the list is inverted.
 *
 * The library re-renders a row only when its data changes, so the live parts of a turn — the activity line and the
 * agent steps — ride on the reply in progress and nowhere else: a new status re-renders that one row, not the list.
 */
export function toConversationRows(state: Pick<ChatState, 'messages' | 'activeTurn' | 'activity' | 'agentSteps'>): ConversationRow[] {
  const streamingId = state.activeTurn?.assistantId
  const rows: ConversationRow[] = []
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const message = state.messages[index]!
    const live = message.id === streamingId
    rows.push({
      _id: message.id,
      text: message.content,
      createdAt: new Date(message.createdAt),
      user: message.role === 'user' ? PERSON : TRINDATRON,
      streaming: message.state === 'streaming',
      message,
      activity: live ? state.activity : null,
      agentSteps: live ? state.agentSteps : NO_STEPS,
      latest: index === state.messages.length - 1,
    })
  }
  return rows
}
