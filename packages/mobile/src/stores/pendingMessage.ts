import { create } from 'zustand'

/**
 * Hands the first message typed on Home to the chat screen it opens, exactly
 * once. Kept out of route params so a reload or back-navigation can never
 * send it twice.
 */
interface PendingMessageState {
  bySession: Record<string, string>
  put(sessionId: string, content: string): void
  take(sessionId: string): string | null
}

export const usePendingMessageStore = create<PendingMessageState>()((set, get) => ({
  bySession: {},
  put(sessionId, content) {
    set((state) => ({ bySession: { ...state.bySession, [sessionId]: content } }))
  },
  take(sessionId) {
    const content = get().bySession[sessionId] ?? null
    if (content !== null) {
      set((state) => {
        const next = { ...state.bySession }
        delete next[sessionId]
        return { bySession: next }
      })
    }
    return content
  },
}))
