import { create } from 'zustand'
import type { Session, Message, AgentStep } from '../types'

const STREAMING_ID = '__streaming__'

interface AuthUser {
  email: string
  plan: string
}

interface AppState {
  // Auth
  token: string | null
  user: AuthUser | null

  // Sessions
  sessions: Session[]
  currentSessionId: string | null

  // Messages for current session
  messages: Message[]
  isStreaming: boolean

  // Agent step trace (ephemeral — cleared on new stream)
  agentSteps: AgentStep[]

  // ── Auth actions ─────────────────────────────────────────────────────────
  setAuth(token: string, user: AuthUser): void
  clearAuth(): void

  // ── Session actions ──────────────────────────────────────────────────────
  setSessions(sessions: Session[]): void
  prependSession(session: Session): void
  removeSession(id: string): void
  patchSession(id: string, updates: Partial<Session>): void
  selectSession(id: string | null): void
  setMessages(messages: Message[]): void

  // ── Chat streaming ───────────────────────────────────────────────────────
  addUserMessage(content: string): void
  beginStreaming(): void
  appendStreamChunk(chunk: string): void
  finalizeStream(messageId: string): void
  setStreamError(message: string): void

  // ── Agent steps ──────────────────────────────────────────────────────────
  addAgentStep(step: AgentStep): void
  clearAgentSteps(): void
}

export const useAppStore = create<AppState>((set) => ({
  token: null,
  user: null,
  sessions: [],
  currentSessionId: null,
  messages: [],
  isStreaming: false,
  agentSteps: [],

  setAuth: (token, user) => set({ token, user }),
  clearAuth: () =>
    set({ token: null, user: null, sessions: [], currentSessionId: null, messages: [] }),

  setSessions: (sessions) => set({ sessions }),
  prependSession: (session) => set((s) => ({ sessions: [session, ...s.sessions] })),
  removeSession: (id) =>
    set((s) => ({
      sessions: s.sessions.filter((sess) => sess.id !== id),
      currentSessionId: s.currentSessionId === id ? null : s.currentSessionId,
      messages: s.currentSessionId === id ? [] : s.messages,
    })),
  patchSession: (id, updates) =>
    set((s) => ({
      sessions: s.sessions.map((sess) => (sess.id === id ? { ...sess, ...updates } : sess)),
    })),

  selectSession: (id) => set({ currentSessionId: id, messages: [] }),
  setMessages: (messages) => set({ messages }),

  addUserMessage: (content) =>
    set((s) => ({
      messages: [
        ...s.messages,
        {
          id: `user-${Date.now()}`,
          role: 'user',
          content,
          created_at: new Date().toISOString(),
        },
      ],
    })),

  addAgentStep: (step) => set((s) => ({ agentSteps: [...s.agentSteps, step] })),
  clearAgentSteps: () => set({ agentSteps: [] }),

  beginStreaming: () =>
    set((s) => ({
      isStreaming: true,
      agentSteps: [],
      messages: [
        ...s.messages,
        {
          id: STREAMING_ID,
          role: 'assistant',
          content: '',
          created_at: new Date().toISOString(),
          isStreaming: true,
        },
      ],
    })),

  appendStreamChunk: (chunk) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === STREAMING_ID ? { ...m, content: m.content + chunk } : m,
      ),
    })),

  finalizeStream: (messageId) =>
    set((s) => ({
      isStreaming: false,
      messages: s.messages.map((m) =>
        m.id === STREAMING_ID ? { ...m, id: messageId, isStreaming: false } : m,
      ),
    })),

  setStreamError: (message) =>
    set((s) => ({
      isStreaming: false,
      messages: s.messages.map((m) =>
        m.id === STREAMING_ID
          ? { ...m, id: `err-${Date.now()}`, content: `⚠ ${message}`, isStreaming: false }
          : m,
      ),
    })),
}))
