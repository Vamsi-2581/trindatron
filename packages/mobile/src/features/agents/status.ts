import type { BadgeTone } from '@/components/ui/Badge'
import type { AgentInstanceStatus, ExecutionJob, ExecutionJobStatus } from '@/services/api/types'
import { safeUserMessage } from '@/services/api/redact'

export const JOB_STATUS_TONE: Record<ExecutionJobStatus, BadgeTone> = {
  created: 'neutral',
  queued: 'neutral',
  starting: 'accent',
  running: 'accent',
  waiting: 'warning',
  completed: 'success',
  failed: 'error',
  cancelled: 'neutral',
  expired: 'warning',
  cleanup: 'neutral',
  destroyed: 'neutral',
}

export const INSTANCE_STATUS_TONE: Record<AgentInstanceStatus, BadgeTone> = {
  active: 'success',
  paused: 'neutral',
  disabled: 'neutral',
  error: 'error',
}

export const jobStatusKey = (status: ExecutionJobStatus) => `agents.jobStatus.${status}`
export const instanceStatusKey = (status: AgentInstanceStatus) => `agents.instanceStatus.${status}`

const RESULT_TEXT_KEYS = ['answer', 'summary', 'output', 'message', 'text'] as const

/**
 * A job's result is internal JSON that may carry routing or model details.
 * Only a plain human-readable answer is ever shown, and only if it names
 * nothing internal.
 */
export function jobResultText(result: ExecutionJob['result']): string | null {
  if (!result) return null
  for (const key of RESULT_TEXT_KEYS) {
    const value = result[key]
    if (typeof value === 'string' && value.trim()) {
      const safe = safeUserMessage(value, '')
      return safe || null
    }
  }
  return null
}
