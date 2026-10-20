import { t } from '@/i18n'
import { formatTimeUntil } from '@/utils/dates'

import { safeUserMessage } from './redact'

export type ApiErrorKind =
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'validation'
  | 'payload_too_large'
  | 'unsupported_media'
  | 'quota_exceeded'
  | 'rate_limited'
  | 'dependency_unavailable'
  | 'server'
  | 'network'
  | 'timeout'
  | 'aborted'
  | 'config'

interface ApiErrorInit {
  kind: ApiErrorKind
  status: number | null
  message: string
  code?: string | null
  resetsAt?: string | null
  detail?: unknown
}

export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly status: number | null
  /** Machine-readable error id from the Core, e.g. "daily_limit_exceeded". */
  readonly code: string | null
  /** ISO instant when a quota renews, when the Core says so. */
  readonly resetsAt: string | null
  readonly detail: unknown

  constructor(init: ApiErrorInit) {
    super(init.message)
    this.name = 'ApiError'
    this.kind = init.kind
    this.status = init.status
    this.code = init.code ?? null
    this.resetsAt = init.resetsAt ?? null
    this.detail = init.detail
  }
}

export function fallbackMessage(kind: ApiErrorKind): string {
  return t(`error.api.${kind}`)
}

/**
 * Core messages the user may see, translated. The Core writes free text in a
 * mix of languages; anything not listed here is replaced by the localized
 * message for its error kind rather than shown verbatim.
 */
const CORE_MESSAGE_KEYS: Record<string, string> = {
  'An internal error occurred.': 'error.api.server',
  'AI service temporarily unavailable. Please try again.': 'error.api.dependency_unavailable',
  'Too many login attempts. Please wait and try again.': 'error.core.too_many_logins',
  'Too many accounts created from this network. Please try again later.': 'error.core.too_many_signups',
  'Password must be at least 8 characters': 'error.core.password_too_short',
  'Invalid or expired token': 'auth.sessionExpired',
  'User not found or account inactive': 'auth.sessionExpired',
}

export function localizeCoreMessage(message: string | null | undefined): string | null {
  const key = message ? CORE_MESSAGE_KEYS[message.trim()] : undefined
  return key ? t(key) : null
}

function kindForStatus(status: number, detail: unknown): ApiErrorKind {
  if (status === 401) return 'unauthorized'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 409) return 'conflict'
  if (status === 413) return 'payload_too_large'
  if (status === 415) return 'unsupported_media'
  if (status === 400 || status === 422) return 'validation'
  // A quota rejection carries a structured detail with a renewal instant; a
  // plain string detail is a request-rate limit (login attempts, concurrency).
  if (status === 429) return isRecord(detail) ? 'quota_exceeded' : 'rate_limited'
  if (status === 424 || status === 503) return 'dependency_unavailable'
  return 'server'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Extracts the human message from FastAPI's `detail`, in any of its shapes. */
export function messageFromDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail || null
  if (Array.isArray(detail)) {
    const first = detail[0]
    if (isRecord(first) && typeof first.msg === 'string') {
      return first.msg.replace(/^Value error,\s*/i, '')
    }
    return null
  }
  if (isRecord(detail) && typeof detail.message === 'string') return detail.message
  return null
}

function quotaMessage(resetsAt: string | null): string {
  if (!resetsAt) return fallbackMessage('quota_exceeded')
  return t('error.api.quota_exceeded_until', { when: formatTimeUntil(resetsAt) })
}

/**
 * Structured refusals from creations, conversation files and teams carry a
 * stable `error` code; the app shows its own translation of that code.
 */
const CORE_ERROR_CODES = new Set([
  'daily_limit_reached',
  'voice_request_too_large',
  'budget_exhausted',
  'plan_not_included',
  'media_unavailable',
  'job_in_progress',
  'too_large',
  'too_many',
  'unavailable',
  'already_in_team',
  'team_not_active',
  'no_seat_available',
  'individual_plan_active',
  'seats_in_use',
  'invite_expired',
  'owner_cannot_leave',
  'invalid_seats',
])

export function errorFromResponse(status: number, body: unknown): ApiError {
  const detail = isRecord(body) ? body.detail : undefined
  const record = isRecord(detail) ? detail : null
  const code = record && typeof record.error === 'string' ? record.error : null
  // A daily creation limit is a quota too, even though its detail is a structured refusal.
  const kind = code === 'daily_limit_reached' ? 'quota_exceeded' : kindForStatus(status, detail)
  const resetsAt = record && typeof record.resets_at === 'string' ? record.resets_at : null
  const message =
    code && CORE_ERROR_CODES.has(code) && code !== 'daily_limit_reached'
      ? t(`error.code.${code}`)
      : kind === 'quota_exceeded'
        ? quotaMessage(resetsAt)
        : safeUserMessage(localizeCoreMessage(messageFromDetail(detail)), fallbackMessage(kind))
  return new ApiError({
    kind,
    status,
    message,
    code,
    resetsAt,
    detail,
  })
}

/** Message for an `error` event inside a chat stream. */
export function streamErrorMessage(message: string | null | undefined, code: string | null): string {
  if (code === 'quality_not_entitled') return fallbackMessage('forbidden')
  return safeUserMessage(localizeCoreMessage(message), fallbackMessage('server'))
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  return new ApiError({ kind: 'network', status: null, message: fallbackMessage('network'), detail: error })
}

export function userMessage(error: unknown): string {
  return toApiError(error).message
}
