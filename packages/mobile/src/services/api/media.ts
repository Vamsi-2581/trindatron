import { apiRequest, authHeaders, buildUrl } from './client'

/** Only abstract fields are declared; nothing the Core could add about engines or cost is read. */
export interface ArtifactView {
  id: string
  kind: 'image' | 'video' | 'avatar' | 'document' | 'agent_output'
  content_type: string
  size_bytes: number
  width: number | null
  height: number | null
  duration_seconds: number | null
  created_at: string
  expires_at: string | null
  content_path: string
  download_path: string
}

export type MediaJobStatus = 'queued' | 'running' | 'completed' | 'failed'

export interface MediaJobView {
  id: string
  kind: 'image' | 'video' | 'avatar'
  operation: 'generate' | 'edit' | 'image_to_video'
  status: MediaJobStatus
  progress: number | null
  error_code: string | null
  project_id: string | null
  created_at: string
  completed_at: string | null
  artifact: ArtifactView | null
}

export interface CreationList {
  creations: MediaJobView[]
  next_before: string | null
}

export interface CreateMediaInput {
  prompt: string
  /** Any format in the person's words: "1080x1920", "9:16", "Instagram Story". The Core parses and fits it. */
  format?: string | null
  projectId?: string | null
  durationSeconds?: number | null
}

export function createImage(input: CreateMediaInput, idempotencyKey: string): Promise<MediaJobView> {
  return apiRequest<MediaJobView>('/media/images', {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: { prompt: input.prompt, format: input.format ?? null, project_id: input.projectId ?? null },
  })
}

export function createVideo(input: CreateMediaInput, idempotencyKey: string): Promise<MediaJobView> {
  return apiRequest<MediaJobView>('/media/videos', {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: {
      prompt: input.prompt,
      format: input.format ?? null,
      project_id: input.projectId ?? null,
      ...(input.durationSeconds ? { duration_seconds: input.durationSeconds } : {}),
    },
  })
}

export function getMediaJob(jobId: string, signal?: AbortSignal): Promise<MediaJobView> {
  return apiRequest<MediaJobView>(`/media/jobs/${encodeURIComponent(jobId)}`, { signal })
}

export function listCreations(filter: { kind?: 'image' | 'video' | null; before?: string | null; limit?: number } = {}, signal?: AbortSignal): Promise<CreationList> {
  const query = new URLSearchParams()
  if (filter.kind) query.set('kind', filter.kind)
  if (filter.before) query.set('before', filter.before)
  query.set('limit', String(filter.limit ?? 30))
  return apiRequest<CreationList>(`/media/creations?${query.toString()}`, { signal })
}

export function deleteCreation(jobId: string): Promise<void> {
  return apiRequest<void>(`/media/creations/${encodeURIComponent(jobId)}`, { method: 'DELETE' })
}

/** An image source that carries the session: created files are only readable by their owner. */
export function authorizedSource(path: string): { uri: string; headers: Record<string, string> } {
  return { uri: buildUrl(path), headers: authHeaders(true) }
}
