'use client'

import { create } from 'zustand'
import type { Session, Message, AgentStep, Project, Skill, AgentDefinition, Entitlement, ApprovalRequiredEvent } from '../types'

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

  // Activity Stream — operational status messages during streaming
  statusMessages: string[]

  // Agent step trace (ephemeral — cleared on new stream)
  agentSteps: AgentStep[]

  // Pending CONFIRM-level tool approval gate (write_file/execute_shell) —
  // ephemeral, cleared on new stream / after resolution, same lifecycle
  // as agentSteps/statusMessages above.
  pendingApproval: ApprovalRequiredEvent | null

  // Pending message for new session (set on home, consumed in session page)
  pendingMessage: string | null

  // ── Auth ──────────────────────────────────────────────────────────────────
  setAuth(token: string, user: AuthUser): void
  clearAuth(): void

  // ── Sessions ──────────────────────────────────────────────────────────────
  setSessions(sessions: Session[]): void
  prependSession(session: Session): void
  removeSession(id: string): void
  patchSession(id: string, updates: Partial<Session>): void
  selectSession(id: string | null): void
  setMessages(messages: Message[]): void

  // ── Chat streaming ────────────────────────────────────────────────────────
  addUserMessage(content: string): void
  beginStreaming(): void
  appendStreamChunk(chunk: string): void
  /** The reply being streamed started a creation; its card appears in that reply. */
  attachMediaToStreaming(jobId: string): void
  /** The reply being streamed started a job (a presentation); its file card appears in that reply. */
  attachJobToStreaming(jobId: string): void
  finalizeStream(messageId: string, userMessageId?: string): void
  setStreamError(message: string): void

  // ── Activity Stream ───────────────────────────────────────────────────────
  addStatusMessage(message: string): void
  clearStatusMessages(): void

  // ── Agent steps ───────────────────────────────────────────────────────────
  addAgentStep(step: AgentStep): void
  clearAgentSteps(): void

  // ── Agent approval gate ───────────────────────────────────────────────────
  setPendingApproval(event: ApprovalRequiredEvent | null): void

  // ── Navigation state ──────────────────────────────────────────────────────
  setPendingMessage(message: string | null): void

  // ── Projects ──────────────────────────────────────────────────────────────
  projects: Project[]
  setProjects(projects: Project[]): void
  prependProject(project: Project): void
  updateProject(project: Project): void
  removeProject(id: string): void

  // ── Skills ────────────────────────────────────────────────────────────────
  skills: Skill[]
  setSkills(skills: Skill[]): void

  // ── Agent Definitions ─────────────────────────────────────────────────────
  agentDefinitions: AgentDefinition[]
  setAgentDefinitions(agents: AgentDefinition[]): void
  prependAgentDefinition(agent: AgentDefinition): void
  updateAgentDefinition(agent: AgentDefinition): void
  removeAgentDefinition(id: string): void

  // ── Entitlement ───────────────────────────────────────────────────────────
  entitlement: Entitlement | null
  setEntitlement(entitlement: Entitlement): void
}

export const useAppStore = create<AppState>((set) => ({
  token: null,
  user: null,
  sessions: [],
  currentSessionId: null,
  messages: [],
  isStreaming: false,
  statusMessages: [],
  agentSteps: [],
  pendingApproval: null,
  pendingMessage: null,
  projects: [],
  skills: [],
  agentDefinitions: [],
  entitlement: null,

  setAuth: (token, user) => set({ token, user }),
  clearAuth: () =>
    set({ token: null, user: null, sessions: [], currentSessionId: null, messages: [], statusMessages: [], entitlement: null }),

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

  beginStreaming: () =>
    set((s) => ({
      isStreaming: true,
      agentSteps: [],
      statusMessages: [],
      pendingApproval: null,
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

  attachMediaToStreaming: (jobId) =>
    set((s) => ({
      messages: s.messages.map((m) => (m.id === STREAMING_ID ? { ...m, media_job_id: jobId } : m)),
    })),

  attachJobToStreaming: (jobId) =>
    set((s) => ({
      messages: s.messages.map((m) => (m.id === STREAMING_ID ? { ...m, execution_job_id: jobId } : m)),
    })),

  finalizeStream: (messageId, userMessageId) =>
    set((s) => ({
      isStreaming: false,
      statusMessages: [],
      pendingApproval: null,
      messages: s.messages.map((m) => {
        if (m.id === STREAMING_ID) return { ...m, id: messageId, isStreaming: false }
        if (userMessageId && m.role === 'user' && m.id.startsWith('user-')) {
          return { ...m, id: userMessageId }
        }
        return m
      }),
    })),

  setStreamError: (message) =>
    set((s) => ({
      isStreaming: false,
      statusMessages: [],
      pendingApproval: null,
      messages: s.messages.map((m) =>
        m.id === STREAMING_ID
          ? { ...m, id: `err-${Date.now()}`, content: `⚠ ${message}`, isStreaming: false }
          : m,
      ),
    })),

  addStatusMessage: (message) =>
    set((s) => ({ statusMessages: [...s.statusMessages.slice(-4), message] })),
  clearStatusMessages: () => set({ statusMessages: [] }),

  addAgentStep: (step) => set((s) => ({ agentSteps: [...s.agentSteps, step] })),
  clearAgentSteps: () => set({ agentSteps: [] }),

  setPendingApproval: (event) => set({ pendingApproval: event }),

  setPendingMessage: (message) => set({ pendingMessage: message }),

  setProjects: (projects) => set({ projects }),
  prependProject: (project) => set((s) => ({ projects: [project, ...s.projects] })),
  updateProject: (project) =>
    set((s) => ({ projects: s.projects.map((p) => (p.id === project.id ? project : p)) })),
  removeProject: (id) => set((s) => ({ projects: s.projects.filter((p) => p.id !== id) })),

  setSkills: (skills) => set({ skills }),

  setAgentDefinitions: (agentDefinitions) => set({ agentDefinitions }),
  prependAgentDefinition: (agent) => set((s) => ({ agentDefinitions: [agent, ...s.agentDefinitions] })),
  updateAgentDefinition: (agent) =>
    set((s) => ({ agentDefinitions: s.agentDefinitions.map((a) => (a.id === agent.id ? agent : a)) })),
  removeAgentDefinition: (id) => set((s) => ({ agentDefinitions: s.agentDefinitions.filter((a) => a.id !== id) })),

  setEntitlement: (entitlement) => set({ entitlement }),
}))
