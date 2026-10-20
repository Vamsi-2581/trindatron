import { describe, it, expect, beforeEach } from 'vitest'
import { useAppStore } from '../store/useAppStore'

describe('useAppStore streaming state', () => {
  beforeEach(() => {
    useAppStore.setState({
      isStreaming: false,
      messages: [],
      statusMessages: [],
      agentSteps: [],
    })
  })

  it('isStreaming is false initially', () => {
    expect(useAppStore.getState().isStreaming).toBe(false)
  })

  it('beginStreaming sets isStreaming true and appends placeholder message', () => {
    useAppStore.getState().beginStreaming()
    const state = useAppStore.getState()
    expect(state.isStreaming).toBe(true)
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('')
    expect(state.messages[0].isStreaming).toBe(true)
  })

  it('appendStreamChunk accumulates content', () => {
    useAppStore.getState().beginStreaming()
    useAppStore.getState().appendStreamChunk('Hello')
    useAppStore.getState().appendStreamChunk(' world')
    const msg = useAppStore.getState().messages.find((m) => m.isStreaming)
    expect(msg?.content).toBe('Hello world')
  })

  it('finalizeStream sets isStreaming false and replaces placeholder id', () => {
    useAppStore.getState().beginStreaming()
    useAppStore.getState().appendStreamChunk('Done')
    useAppStore.getState().finalizeStream('msg-abc')
    const state = useAppStore.getState()
    expect(state.isStreaming).toBe(false)
    const msg = state.messages.find((m) => m.id === 'msg-abc')
    expect(msg).toBeDefined()
    expect(msg?.content).toBe('Done')
    expect(msg?.isStreaming).toBe(false)
  })

  it('setStreamError sets isStreaming false and marks error in content', () => {
    useAppStore.getState().beginStreaming()
    useAppStore.getState().setStreamError('Network failure')
    const state = useAppStore.getState()
    expect(state.isStreaming).toBe(false)
    expect(state.messages[0].content).toContain('Network failure')
  })
})
