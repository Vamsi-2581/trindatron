export interface User {
  id: string
  email: string
  full_name: string
  plan: string
  is_active: boolean
}

export interface Session {
  id: string
  title: string
  model_default: string
  created_at: string
  updated_at: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  isStreaming?: boolean
}

export interface DoneEvent {
  type: 'done'
  session_id: string
  message_id: string
  usage: {
    input_tokens: number
    output_tokens: number
    cached_tokens: number
    cost_usd: number
    tcu: number
    duration_ms: number
    model: string
  }
}

export interface AgentStep {
  type: 'tool_call' | 'tool_result'
  tool: string
  args?: Record<string, unknown>
  success?: boolean
  output?: string
}

export interface AgentDoneEvent {
  type: 'done'
  session_id: string
  message_id: string
  answer: string
  iterations: number
  usage: {
    input_tokens: number
    output_tokens: number
    cost_usd: number
    tcu: number
    duration_ms: number
  }
}

export interface StatusEvent {
  type: 'status'
  key: string
  message: string
}
