import type { Entitlement } from '@/services/api/types'

/**
 * Product capabilities as the app understands them — abstract names only.
 * Which engine fulfils one, and at what cost, is decided and hidden by
 * TRINITY CORE; nothing here (or in the contract below) names a vendor.
 *
 * The Core answers GET /users/me/capabilities with two views of the same
 * truth: `capabilities`, a per-feature dictionary carrying today's allowances,
 * and `matrix`, which says WHY something is off — not in the plan, no engine
 * reachable, or never integrated. The app used to read only the first, so
 * every unavailable capability looked identical on screen and half the matrix
 * (the browser, the virtual computer, reading a file, hearing an answer) was
 * invisible to it. Both are read here, and the reason survives to the UI.
 */
export const MEDIA_CAPABILITIES = [
  'image_generation',
  'image_editing',
  'video_generation',
  'image_to_video',
  'avatar_generation',
] as const
export type MediaCapability = (typeof MEDIA_CAPABILITIES)[number]

export const PRODUCT_CAPABILITIES = [
  'voice_input',
  'audio_output',
  'agent_mode',
  'persistent_agents',
  'presentations',
  'project_files',
  'chat_attachments',
  'image_understanding',
  'browser_control',
  'computer_control',
] as const
export type ProductCapability = (typeof PRODUCT_CAPABILITIES)[number]

export type CapabilityId = MediaCapability | ProductCapability

const ALL_CAPABILITIES: readonly CapabilityId[] = [...MEDIA_CAPABILITIES, ...PRODUCT_CAPABILITIES]

/**
 * Why a capability is not usable, in the Core's own words:
 *   available      — usable right now
 *   plan           — real, but not included in this plan
 *   unavailable    — in the plan, not reachable at the moment
 *   not_integrated — does not exist in the product yet
 * A screen that shows all four the same way tells the person nothing.
 */
export const CAPABILITY_STATUSES = ['available', 'plan', 'unavailable', 'not_integrated'] as const
export type CapabilityStatus = (typeof CAPABILITY_STATUSES)[number]

/** A daily allowance exactly as the Core reports it. The app never counts or enforces usage itself. */
export interface DailyQuota {
  limit: number
  remaining: number
  resetsAt: string | null
}

/** Per-plan media limits, as the Core reports them. */
export interface MediaLimits {
  qualityTier: string | null
  maxResolution: string | null
  maxDurationSeconds: number | null
}

export interface CapabilityState {
  available: boolean
  /** Absent in a hand-built state (tests, the baseline): derived from `available`. */
  status?: CapabilityStatus
  dailyQuota: DailyQuota | null
  limits: MediaLimits | null
  /** "sandbox" for things that run in a separate environment, never on the person's own device. */
  scope?: string | null
}

export type Capabilities = Record<CapabilityId, CapabilityState>

const OFF: CapabilityState = { available: false, status: 'unavailable', dailyQuota: null, limits: null }
const ON: CapabilityState = { available: true, status: 'available', dailyQuota: null, limits: null }

/** The reason a capability is off, for a state that may predate the matrix. */
export function statusOf(state: CapabilityState | undefined): CapabilityStatus {
  if (!state) return 'not_integrated'
  return state.status ?? (state.available ? 'available' : 'unavailable')
}

/**
 * What works for every signed-in user regardless of plan: voice input goes
 * through POST /voice/transcribe and files through project storage. Everything
 * else starts unavailable until the Core says otherwise.
 */
export function baselineCapabilities(): Capabilities {
  const result = Object.fromEntries(ALL_CAPABILITIES.map((id) => [id, OFF])) as Capabilities
  return { ...result, voice_input: ON, project_files: ON }
}

/**
 * GET /users/me/capabilities — the Core's single answer to "what can this user
 * do right now" (plan, operator switches, engine and storage availability,
 * today's allowances). When it cannot be reached the app falls back to the
 * entitlement and never guesses media availability.
 */
export const CORE_CAPABILITIES_PATH: string | null = '/users/me/capabilities'

export function capabilitiesFromEntitlement(entitlement: Entitlement, extra: { presentations: boolean }): Capabilities {
  const planned = (included: boolean): CapabilityState =>
    included ? ON : { available: false, status: 'plan', dailyQuota: null, limits: null }
  return {
    ...baselineCapabilities(),
    agent_mode: planned(entitlement.features.supports_agent_mode),
    persistent_agents: planned(entitlement.limits.max_agents !== 0),
    presentations: extra.presentations ? ON : OFF,
    // Media stays off: a plan flag says nothing about whether an engine can run right now,
    // and only the Core's capability answer may put a creation button on screen.
  }
}

/** Which matrix entry answers for each capability this app names. */
const MATRIX_KEY: Partial<Record<CapabilityId, string>> = {
  image_generation: 'image_generation',
  image_editing: 'image_editing',
  video_generation: 'video_generation',
  image_to_video: 'image_to_video',
  avatar_generation: 'avatar_generation',
  image_understanding: 'vision',
  chat_attachments: 'file_analysis',
  presentations: 'document_generation',
  voice_input: 'transcription',
  audio_output: 'audio_output',
  agent_mode: 'agent',
  browser_control: 'browser_control',
  computer_control: 'computer_control',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : null
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function statusFrom(entry: Record<string, unknown> | null, available: boolean): CapabilityStatus {
  const raw = entry ? text(entry.status) : null
  return (CAPABILITY_STATUSES as readonly string[]).includes(raw ?? '')
    ? (raw as CapabilityStatus)
    : available
      ? 'available'
      : 'unavailable'
}

function quotaFrom(entry: Record<string, unknown> | null): DailyQuota | null {
  const quota = entry && isRecord(entry.daily_quota) ? entry.daily_quota : null
  const limit = quota ? count(quota.limit) : null
  const remaining = quota ? count(quota.remaining) : null
  if (limit === null || remaining === null) return null
  return { limit, remaining: Math.min(remaining, limit), resetsAt: text(quota?.resets_at) }
}

function limitsFrom(...sources: (Record<string, unknown> | null)[]): MediaLimits | null {
  for (const source of sources) {
    const limits = source && isRecord(source.limits) ? source.limits : null
    if (!limits) continue
    return {
      qualityTier: text(limits.quality_tier),
      maxResolution: text(limits.max_resolution),
      maxDurationSeconds: count(limits.max_duration_seconds),
    }
  }
  return null
}

/**
 * Reads the Core's answer. Only the fields below are ever read, so anything
 * else the Core sends (an engine name, a cost) cannot reach the screen.
 *
 * { "capabilities": { "image_generation": { "available": true,
 *       "daily_quota": { "limit": 10, "remaining": 7, "resets_at": "..." },
 *       "limits": { "quality_tier": "standard", "max_resolution": "1024x1024" } } },
 *   "matrix": { "image_generation": { "available": true, "status": "available",
 *       "scope": "sandbox", "limits": { "daily_quota": { ... } } } } }
 *
 * Either half alone is enough: an older Core that sends no matrix still works,
 * and a capability present only in the matrix (the browser, the virtual
 * computer, hearing an answer) is read from it.
 */
export function parseCoreCapabilities(body: unknown): Capabilities | null {
  if (!isRecord(body)) return null
  const legacy = isRecord(body.capabilities) ? body.capabilities : null
  const matrix = isRecord(body.matrix) ? body.matrix : null
  if (!legacy && !matrix) return null

  const result = baselineCapabilities()
  for (const id of ALL_CAPABILITIES) {
    const legacyEntry = legacy && isRecord(legacy[id]) ? (legacy[id] as Record<string, unknown>) : null
    const matrixKey = MATRIX_KEY[id]
    const matrixEntry = matrix && matrixKey && isRecord(matrix[matrixKey]) ? (matrix[matrixKey] as Record<string, unknown>) : null
    if (!legacyEntry && !matrixEntry) continue

    // The matrix decides availability when it answers for this capability; it is the view that
    // knows about engines and operator switches, not only about the plan.
    const available = matrixEntry ? matrixEntry.available === true : legacyEntry?.available === true
    result[id] = {
      available,
      status: statusFrom(matrixEntry, available),
      // Allowances live in the per-feature view; the matrix carries them inside its own limits.
      dailyQuota: quotaFrom(legacyEntry) ?? quotaFrom(isRecord(matrixEntry?.limits) ? (matrixEntry!.limits as Record<string, unknown>) : null),
      limits: limitsFrom(legacyEntry, matrixEntry),
      scope: matrixEntry ? text(matrixEntry.scope) : null,
    }
  }
  return result
}

/** Creation entry points shown in the composer, only for capabilities the Core reports as available. */
export function availableCreations(capabilities: Capabilities): MediaCapability[] {
  return MEDIA_CAPABILITIES.filter((id) => capabilities[id].available)
}
