import { apiRequest } from './client'

/**
 * Agendados: an instruction TRINDATRON carries out on a clock.
 *
 * The Core runs these on the schedule engine it already had — there is no
 * second scheduler here, and nothing about a plan is decided on the device:
 * a plan without agent mode is refused by the Core with `plan_not_included`,
 * and that refusal is what the screen shows.
 */
export type RecurrenceKind = 'hourly' | 'daily' | 'weekly'

export interface Recurrence {
  kind: RecurrenceKind
  /** "HH:MM" for daily and weekly; absent for hourly. */
  time?: string
  /** 0 = Monday … 6 = Sunday, weekly only. */
  weekday?: number
  timezone?: string
}

export interface ScheduledTaskRun {
  job_id: string
  status: string
  completed_at: string | null
  excerpt: string | null
}

export interface ScheduledTask {
  id: string
  title: string
  instruction: string
  recurrence: Recurrence
  is_active: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_outcome: string | null
  last_run: ScheduledTaskRun | null
  created_at: string | null
}

export function listScheduledTasks(signal?: AbortSignal): Promise<{ tasks: ScheduledTask[] }> {
  return apiRequest<{ tasks: ScheduledTask[] }>('/scheduled-tasks', { signal })
}

export function createScheduledTask(input: {
  title: string
  instruction: string
  recurrence: Recurrence
}): Promise<ScheduledTask> {
  return apiRequest<ScheduledTask>('/scheduled-tasks', { method: 'POST', body: input })
}

export function updateScheduledTask(
  taskId: string,
  changes: { title?: string; instruction?: string; is_active?: boolean; recurrence?: Recurrence },
): Promise<ScheduledTask> {
  return apiRequest<ScheduledTask>(`/scheduled-tasks/${encodeURIComponent(taskId)}`, { method: 'PATCH', body: changes })
}

export function deleteScheduledTask(taskId: string): Promise<void> {
  return apiRequest<void>(`/scheduled-tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' })
}

/** Runs it once now, through the same runtime. 409 when one is already running. */
export function runScheduledTask(taskId: string): Promise<{ job_id: string; status: string }> {
  return apiRequest<{ job_id: string; status: string }>(`/scheduled-tasks/${encodeURIComponent(taskId)}/run`, {
    method: 'POST',
  })
}
