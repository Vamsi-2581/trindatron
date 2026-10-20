/**
 * Wire types for TRINITY CORE. Mirrors packages/core/src/schemas/* and
 * packages/web/src/types.ts — the Core is the source of truth. Only fields the
 * Core actually returns belong here.
 */

export interface AuthTokenResponse {
  access_token: string
  token_type: string
  user_id: string
  email: string
  /** Present for clients that declare themselves (client: "mobile"): rotates on every refresh. */
  refresh_token?: string | null
  /** Seconds the access token stays valid. */
  expires_in?: number | null
}

export interface User {
  id: string
  email: string
  full_name: string
  plan: string
  is_active: boolean
  timezone: string | null
  created_at: string
}

/*
 * Fields that name a model, a provider or a cost (model_default, model_used,
 * provider_name, token/TCU usage) are deliberately not declared: the Core may
 * send them, but nothing in the app can read — and so show — them.
 */

export interface Session {
  id: string
  title: string
  project_id: string | null
  agent_definition_id: string | null
  is_temporary: boolean
  created_at: string
  updated_at: string
}

export interface SessionList {
  sessions: Session[]
  total: number
}

export type MessageRole = 'user' | 'assistant' | 'system'

export interface Message {
  id: string
  session_id: string
  role: MessageRole
  content: string
  created_at: string
  /** The creation this reply started (a request like "crie uma imagem…"). */
  media_job_id?: string | null
  /** The reply stopped at the plan's output limit instead of finishing. */
  truncated?: boolean
}

export interface SessionDetail {
  id: string
  title: string
  project_id?: string | null
  messages: Message[]
  created_at: string
  updated_at: string
}

/** Returned instead of a stream when an Idempotency-Key was already completed. */
export interface IdempotentReplay {
  idempotent_replay: true
  session_id: string
  message_id: string
  status: 'completed'
}

export interface DoneEvent {
  type: 'done'
  session_id: string
  message_id: string
  user_message_id?: string | null
  /** Present only when the agent pipeline answered. */
  answer?: string
  /** The creation this turn started; its progress arrives as an `artifact` event. */
  media_job_id?: string | null
  /** The answer stopped at the plan's output limit instead of finishing. */
  truncated?: boolean
}

export interface AgentStep {
  type: 'tool_call' | 'tool_result'
  tool: string
  args?: Record<string, unknown>
  success?: boolean
  output?: string
}

export interface ApprovalRequiredEvent {
  action_id: string
  action: string
  args: Record<string, unknown>
  description: string
}

export interface ProjectFile {
  id: string
  project_id: string
  name: string
  mime_type: string
  size_bytes: number
  created_at: string
}

export interface Project {
  id: string
  user_id: string
  name: string
  description: string | null
  instructions: string | null
  is_archived: boolean
  created_at: string
  updated_at: string
  files: ProjectFile[]
}

export interface ProjectList {
  projects: Project[]
  total: number
}

export interface EntitlementFeatures {
  supports_agent_mode: boolean
  supports_trinity_code: boolean
  supports_trinity_work: boolean
  supports_image_generation: boolean
}

export interface Entitlement {
  plan: string
  quota: {
    daily_tcu_limit: number | null
    weekly_tcu_limit: number | null
    monthly_tcu_limit: number | null
    max_tcu_per_request: number
  }
  features: EntitlementFeatures
  limits: { max_projects: number | null; max_agents: number | null }
}

/** User-facing capacity. Deliberately carries no TCU, token or cost figure. */
export interface Capacity {
  tier: string
  capacity_remaining_pct: number
  session: { active: boolean; pct_used: number | null; resets_at: string | null }
  weekly: { pct_used: number | null; resets_at: string | null }
  active_agents: number
  optimizing: boolean
}

export interface AgentDefinition {
  id: string
  user_id: string
  name: string
  description: string | null
  avatar_emoji: string | null
  system_instructions: string | null
  skill_ids: string[]
  model_preference: 'power' | 'balanced' | 'fast'
  is_archived: boolean
  created_at: string
  updated_at: string
}

export type ExecutionJobStatus =
  | 'created' | 'queued' | 'starting' | 'running' | 'waiting'
  | 'completed' | 'failed' | 'cancelled' | 'expired' | 'cleanup' | 'destroyed'

export interface ExecutionArtifact {
  id: string
  filename: string
  content_type: string | null
  size_bytes: number
  created_at: string
}

export interface ExecutionJob {
  id: string
  user_id: string
  agent_id: string | null
  session_id: string | null
  project_id: string | null
  job_type: string
  priority: 'critical' | 'high' | 'normal' | 'low'
  status: ExecutionJobStatus
  error: string | null
  result: Record<string, unknown> | null
  checkpoint: Record<string, unknown>
  attempt_number: number
  max_retries: number
  retry_reason: string | null
  sentinel_status: 'pass' | 'fail' | null
  sentinel_report: Record<string, unknown> | null
  timeout_seconds: number
  created_at: string
  started_at: string | null
  completed_at: string | null
  artifacts: ExecutionArtifact[]
}

export type AgentInstanceStatus = 'active' | 'paused' | 'disabled' | 'error'

export interface AgentInstance {
  id: string
  user_id: string
  agent_definition_id: string
  project_id: string | null
  name: string
  status: AgentInstanceStatus
  health: 'healthy' | 'degraded' | 'failed'
  failure_count: number
  last_error: string | null
  last_run_at: string | null
  next_run_at: string | null
  last_execution_job_id: string | null
  current_execution_job_status: ExecutionJobStatus | null
  last_decision: string | null
  created_at: string
  updated_at: string
}

export interface PlanPrice {
  plan: string
  amount: string
  currency: string
}

export type PaymentOrderStatus =
  | 'pending' | 'processing' | 'confirmed' | 'cancelled'
  | 'failed' | 'expired' | 'refunded' | 'partial_refund'

export interface PaymentOrder {
  id: string
  user_id: string
  provider: string
  external_order_id: string | null
  status: PaymentOrderStatus
  amount_fiat: string
  currency_fiat: string
  credits_amount: string
  currency_credits: string
  idempotency_key: string
  expires_at: string
  confirmed_at: string | null
  plan: string | null
  /** Set when the order paid a recurring subscription cycle; the only way the app learns a subscription's id. */
  subscription_id?: string | null
  created_at: string
  updated_at: string
}

export type SubscriptionStatus = 'active' | 'past_due' | 'grace_period' | 'cancelled' | 'expired'

/** GET /billing/subscriptions/{id} (SubscriptionResponse in the Core). */
export interface Subscription {
  id: string
  plan: string
  provider: string
  payment_method: string
  payment_type: string
  currency: string
  amount: string
  status: SubscriptionStatus
  current_period_start: string | null
  current_period_end: string | null
  next_billing_at: string | null
  cancelled_at: string | null
  grace_period_ends_at: string | null
  created_at: string
  updated_at: string
}

export interface CheckoutResponse {
  order: PaymentOrder
  redirect_url: string | null
  client_secret: string | null
}

export interface VoiceTranscription {
  text: string
  language: string | null
  duration_seconds: number | null
}
