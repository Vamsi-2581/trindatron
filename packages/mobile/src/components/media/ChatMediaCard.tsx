import { useState } from 'react'

import { creationFromJob, type MediaCreation } from '@/features/media/media'
import { useMediaJob } from '@/features/media/useMediaJob'
import type { MediaJobView } from '@/services/api/media'

import { MediaResultCard } from './MediaResultCard'

function jobFrom(media: MediaCreation): MediaJobView {
  return {
    id: media.id,
    kind: media.kind,
    operation: 'generate',
    status: media.status,
    progress: media.progress,
    error_code: null,
    project_id: null,
    created_at: media.createdAt ?? new Date().toISOString(),
    completed_at: null,
    artifact: null,
  }
}

/** A creation asked for in the conversation, followed until it is ready and shown where it was asked for. */
export function ChatMediaCard({ media }: { media: MediaCreation }) {
  const [initial] = useState(() => jobFrom(media))
  // Only the job id is certain for a card rebuilt from history; ask the Core at once.
  const { job } = useMediaJob(initial, { immediate: true })
  return <MediaResultCard media={job ? creationFromJob(job) : media} />
}
