export interface User {
  id: string
  email: string
  full_name: string
  plan: string
  is_active: boolean
  // IANA timezone (e.g. "America/Sao_Paulo"), or null if not yet detected.
  // Drives the weekly reset rule — see PATCH /users/me/timezone.
  timezone: string | null
  /** True once the owner confirmed the address (confirmation link, password reset or Google). */
  email_verified?: boolean
}

export interface Session {
  id: string
  title: string
  project_id: string | null
  agent_definition_id: string | null
  created_at: string
  updated_at: string
}

export interface Skill {
  id: string
  user_id: string | null
  name: string
  description: string | null
  instructions: string | null
  tool_ids: string[]
  icon_emoji: string | null
  is_builtin: boolean
  created_at: string | null
  updated_at: string | null
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

export interface CodeWorkspace {
  id: string
  project_id: string
  root_path: string | null
  language_hint: string | null
  run_command: string | null
  build_command: string | null
  test_command: string | null
  git_enabled: boolean
  sandbox_image: string | null
  created_at: string
  updated_at: string
}

export interface WorkWorkspace {
  id: string
  project_id: string
  requires_approval: boolean
  approval_policy: string
  created_at: string
  updated_at: string
}

export interface WorkspaceFileEntry {
  name: string
  path: string
  is_dir: boolean
  size: number | null
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  isStreaming?: boolean
  /** The creation this reply started ("crie uma imagem…"); the card follows it. */
  media_job_id?: string | null
  /** The job this reply started (a presentation); its card follows it and offers the file. */
  execution_job_id?: string | null
}

export interface StatusEvent {
  type: 'status'
  key?: string    // semantic i18n key (e.g. "activity.research.searching") — new protocol
  message: string // PT-BR fallback string — backward compat for clients without i18n
}

export interface DoneEvent {
  type: 'done'
  session_id: string
  message_id: string
  user_message_id?: string
  /** A job this turn started (a presentation), followed by its card until the file is ready. */
  job_id?: string
  usage: {
    input_tokens: number
    output_tokens: number
    cached_tokens: number
    duration_ms: number
  }
}

export interface AgentStep {
  type: 'tool_call' | 'tool_result'
  /** i18n key of the activity (e.g. "activity.tool.web_search") — never the
   * internal tool name, which is implementation detail. */
  activity: string
  /** PT-BR fallback for clients without the key. */
  label?: string
  args?: Record<string, unknown>
  success?: boolean
  output?: string
}

/** A CONFIRM-level tool call pausing an SSE stream for explicit user
 * approval — shared shape emitted by both TRINITY WORK and TRINITY
 * AGENT (see api/client.ts's WorkApprovalEvent, kept as an alias for
 * backward compatibility with the WORK page's existing import). */
export interface ApprovalRequiredEvent {
  action_id: string
  activity?: string
  label?: string
  args: Record<string, unknown>
  description: string
}

export interface AgentDoneEvent {
  type: 'done'
  session_id: string
  message_id: string
  user_message_id?: string
  /** A job this turn started (a presentation): the reply's file card follows it. */
  job_id?: string
  answer: string
  iterations: number
  usage: {
    input_tokens: number
    output_tokens: number
    duration_ms: number
  }
}

export interface EntitlementFeatures {
  supports_agent_mode: boolean
  supports_trinity_code: boolean
  supports_trinity_work: boolean
  supports_image_generation: boolean
}

export interface EntitlementQuota {
  daily_tcu_limit: number | null
  weekly_tcu_limit: number | null
  monthly_tcu_limit: number | null
  max_tcu_per_request: number
}

export interface EntitlementLimits {
  max_projects: number | null
  max_agents: number | null
}

export interface Entitlement {
  plan: string
  quota: EntitlementQuota
  features: EntitlementFeatures
  limits: EntitlementLimits
}

export interface UserUsagePeriod {
  used: number
  limit: number | null   // null = unlimited (max/team plans)
  remaining: number | null
}

export interface WeeklyUsagePeriod extends UserUsagePeriod {
  // ISO datetimes in UTC — the backend computes the weekly reset rule
  // (Sunday 12:00 in the user's local IANA timezone); the frontend only
  // converts these to local time for display/countdown, never re-derives
  // the rule itself. See src/timezones.py.
  week_start: string
  next_reset_at: string
}

// 5-hour rolling entitlement session — a separate dimension from daily/
// weekly/credits. The backend is the sole authority: it starts/updates
// this only as a side effect of an operation that actually reserves TCU
// (never on login/app-open). The frontend only reads it for display and
// computes a countdown from session_expires_at — never to authorize or
// block anything client-side. See src/services/session_window_service.py.
export interface SessionStatus {
  active: boolean
  session_started_at: string | null  // ISO UTC, or null if never started
  session_expires_at: string | null  // ISO UTC, or null if never started
}

export interface UserUsage {
  plan: string
  daily: UserUsagePeriod
  weekly: WeeklyUsagePeriod
  // monthly is still returned by the API for backward compatibility, but
  // there is no operational monthly usage cap anymore (product decision) —
  // the panel no longer displays it as a quota. Kept optional/untyped-strict
  // here since the UI must not depend on it.
  monthly: UserUsagePeriod
}

// Execution Runtime V1 — user-facing capacity view. Deliberately carries NO
// TCU/token/cost field anywhere — percentages, a renewal instant, and
// counts only. See GET /users/me/capacity (src/api/routes/users.py) and
// src/services/adaptive_budget_service.py for how each figure is derived.
export interface CapacitySession {
  active: boolean
  pct_used: number | null
  resets_at: string | null
}

export interface CapacityWeekly {
  pct_used: number | null
  resets_at: string | null
}

export interface Capacity {
  tier: string
  capacity_remaining_pct: number
  session: CapacitySession
  weekly: CapacityWeekly
  active_agents: number
  optimizing: boolean
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
export type AgentInstanceHealth = 'healthy' | 'degraded' | 'failed'

/** Persistent Agent Instance V1 — the live, running agent (as opposed to
 * AgentDefinition, a reusable template). Never carries secrets/TCU/cost —
 * see schemas/agent_instance.py's own docstring for why there is nothing
 * to leak here. */
export interface AgentInstance {
  id: string
  user_id: string
  agent_definition_id: string
  project_id: string | null
  name: string
  status: AgentInstanceStatus
  health: AgentInstanceHealth
  failure_count: number
  last_error: string | null
  last_run_at: string | null
  next_run_at: string | null
  last_execution_job_id: string | null
  current_execution_job_status: ExecutionJobStatus | null
  /** Tier 2 (LLM DECIDE) observability — e.g. "acted"/"decide_pending"/
   * "decide_skip"/"skipped". Never a TCU/token/cost figure. */
  last_decision: string | null
  created_at: string
  updated_at: string
}

export interface Memory {
  id: string
  content: string
  source_session_id: string | null
  created_at: string
  updated_at: string
}

// ── Voice ─────────────────────────────────────────────────────────────────
// Emotion/prosody fields are structural placeholders — a real backend
// provider may leave them null. See packages/core/src/capability/multimodal.py
// ProsodySignals: nothing here is inferred client-side.

export interface VoiceTranscription {
  text: string
  language: string | null
  duration_seconds: number | null
  emotion: string | null
  confidence: number | null
  pace: number | null
  urgency: number | null
  prosody: Record<string, unknown> | null
}

// ── Billing ────────────────────────────────────────────────────────────────

export interface CreditBalance {
  user_id: string
  currency: string
  balance: string // Decimal serialised as string by Pydantic v2
}

export interface CreditSummary {
  user_id: string
  balances: CreditBalance[]
}

export type PaymentOrderStatus =
  | 'pending'
  | 'processing'
  | 'confirmed'
  | 'cancelled'
  | 'failed'
  | 'expired'
  | 'refunded'
  | 'partial_refund'

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
  /** Commercial Checkout V1 — the plan this order activates on
   * confirmation, or null for a plain credit-purchase order. */
  plan: string | null
  created_at: string
  updated_at: string
}

export interface PaymentOrderListResponse {
  orders: PaymentOrder[]
  total: number
}

// ── Commercial Checkout V1 ───────────────────────────────────────────────

export interface PlanPrice {
  plan: string
  amount: string // Decimal serialised as string
  currency: string
}

export interface PaymentProvidersResponse {
  providers: string[]
}

export interface CheckoutResponse {
  order: PaymentOrder
  redirect_url: string | null
  client_secret: string | null
}
