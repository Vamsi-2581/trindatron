import type { ChatMessage } from '../chatReducer'
import { PERSON, toConversationRows, TRINDATRON } from '../conversationRows'

const at = '2026-09-16T10:00:00Z'
const question: ChatMessage = { id: 'u1', role: 'user', content: 'Qual a previsão?', createdAt: at, state: 'final' }
const answer: ChatMessage = { id: 'a1', role: 'assistant', content: 'Sol.', createdAt: at, state: 'final' }
const followUp: ChatMessage = { id: 'u2', role: 'user', content: 'E amanhã?', createdAt: at, state: 'final' }
const streaming: ChatMessage = { id: 'a2', role: 'assistant', content: 'Chuv', createdAt: at, state: 'streaming' }

describe('conversation rows', () => {
  it('lists newest first, keeping each message untouched inside its row', () => {
    const rows = toConversationRows({ messages: [question, answer], activeTurn: null, activity: null, agentSteps: [] })

    expect(rows.map((row) => row._id)).toEqual(['a1', 'u1'])
    expect(rows[0]!.message).toBe(answer)
    expect(rows[0]!.user).toBe(TRINDATRON)
    expect(rows[1]!.user).toBe(PERSON)
    expect(rows[1]!.createdAt).toEqual(new Date(at))
  })

  it('puts what the turn is doing on the reply in progress and nowhere else', () => {
    const steps = [{ type: 'tool_call' as const, tool: 'web_search' }]
    const rows = toConversationRows({
      messages: [question, answer, followUp, streaming],
      activeTurn: { userId: 'u2', assistantId: 'a2' },
      activity: 'Pesquisando na web',
      agentSteps: steps,
    })

    const [live, ...settled] = rows
    expect(live!.activity).toBe('Pesquisando na web')
    expect(live!.agentSteps).toBe(steps)
    expect(live!.streaming).toBe(true)
    for (const row of settled) {
      expect(row.activity).toBeNull()
      expect(row.agentSteps).toEqual([])
      expect(row.streaming).toBe(false)
    }
  })

  it('gives settled rows the same empty steps every time, so the list sees them as unchanged', () => {
    const state = { messages: [question, answer], activeTurn: null, activity: null, agentSteps: [] }
    expect(toConversationRows(state)[0]!.agentSteps).toBe(toConversationRows(state)[0]!.agentSteps)
  })
})
