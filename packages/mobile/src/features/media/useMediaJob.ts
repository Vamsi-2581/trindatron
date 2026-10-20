import { useEffect, useState } from 'react'

import { userMessage } from '@/services/api/errors'
import { getMediaJob, type MediaJobView } from '@/services/api/media'

const TERMINAL = new Set(['completed', 'failed'])

interface Followed {
  job: MediaJobView
  error: string | null
}

/**
 * Follows a creation until it finishes. Images change quickly; videos take minutes.
 * `immediate` asks the Core at once — for a job known only by id, whose real state is unknown.
 */
export function useMediaJob(initial: MediaJobView | null, { immediate = false }: { immediate?: boolean } = {}) {
  const [followed, setFollowed] = useState<Followed | null>(null)
  const jobId = initial?.id ?? null
  // Updates for an earlier job never leak into a new one.
  const current = followed && followed.job.id === jobId ? followed : null
  const job = current?.job ?? initial
  const status = job?.status
  const kind = initial?.kind

  useEffect(() => {
    if (!jobId || !status || TERMINAL.has(status)) return
    const controller = new AbortController()
    const interval = kind === 'video' ? 5000 : 2000
    let failures = 0
    let timer: ReturnType<typeof setTimeout> | null = null
    const poll = async () => {
      try {
        const next = await getMediaJob(jobId, controller.signal)
        failures = 0
        setFollowed({ job: next, error: null })
        if (TERMINAL.has(next.status)) return
      } catch (cause) {
        if (controller.signal.aborted) return
        failures += 1
        if (failures >= 3) {
          const message = userMessage(cause)
          setFollowed((previous) => ({ job: previous?.job.id === jobId ? previous.job : initial!, error: message }))
        }
      }
      timer = setTimeout(poll, interval)
    }
    timer = setTimeout(poll, immediate && !current ? 0 : interval)
    return () => {
      controller.abort()
      if (timer) clearTimeout(timer)
    }
    // Restart only for a new job or a status change; `initial` is the same job while jobId is unchanged.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, kind, status])

  return { job, error: current?.error ?? null }
}
