import type { MediaJobView } from '@/services/api/media'

/**
 * A creation (image, video, avatar) as the app shows it. Only these fields are
 * ever read from the Core, so an engine name, a cost or a prompt rewrite the
 * Core might attach can never reach the screen.
 *
 * Creations come from the media jobs API (POST /media/images|videos, GET
 * /media/jobs/{id}); the SSE `artifact` event below is also understood, for a
 * conversation pipeline that streams creations inline.
 */

export const MEDIA_KINDS = ['image', 'video', 'avatar'] as const
export type MediaKind = (typeof MEDIA_KINDS)[number]

export const MEDIA_STATUSES = ['queued', 'running', 'completed', 'failed'] as const
export type MediaStatus = (typeof MEDIA_STATUSES)[number]

export interface MediaCreation {
  id: string
  kind: MediaKind
  status: MediaStatus
  /** 0..1 when the Core reports it. */
  progress: number | null
  previewUrl: string | null
  downloadUrl: string | null
  /** Core path of the stored file, read with the session (owner only). */
  contentPath?: string | null
  /** Core path that serves the same file as an attachment, for saving and sharing. */
  downloadPath?: string | null
  contentType: string | null
  createdAt: string | null
  durationSeconds?: number | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T | null {
  return typeof value === 'string' && (allowed as readonly string[]).includes(value) ? (value as T) : null
}

/** Signed HTTPS links from the Core only (plain HTTP is accepted from a development server). */
export function safeMediaUrl(value: unknown, allowHttp = __DEV__): string | null {
  if (typeof value !== 'string') return null
  const url = value.trim()
  if (/^https:\/\//i.test(url)) return url
  if (allowHttp && /^http:\/\//i.test(url)) return url
  return null
}

export function parseMediaArtifact(value: unknown): MediaCreation | null {
  if (!isRecord(value) || typeof value.id !== 'string' || !value.id) return null
  const kind = oneOf(value.kind, MEDIA_KINDS)
  const status = oneOf(value.status, MEDIA_STATUSES)
  if (!kind || !status) return null
  const progress = typeof value.progress === 'number' && Number.isFinite(value.progress) ? Math.max(0, Math.min(1, value.progress)) : null
  return {
    id: value.id,
    kind,
    status,
    progress,
    previewUrl: safeMediaUrl(value.preview_url),
    downloadUrl: safeMediaUrl(value.download_url),
    contentType: typeof value.content_type === 'string' ? value.content_type : null,
    createdAt: typeof value.created_at === 'string' ? value.created_at : null,
  }
}

/** A media job from the Core, in the shape the result card renders. */
export function creationFromJob(job: MediaJobView): MediaCreation {
  const corePath = (path: unknown) => (typeof path === 'string' && path.startsWith('/media/') ? path : null)
  return {
    id: job.id,
    kind: oneOf(job.kind, MEDIA_KINDS) ?? 'image',
    status: oneOf(job.status, MEDIA_STATUSES) ?? 'failed',
    progress: typeof job.progress === 'number' ? Math.max(0, Math.min(1, job.progress)) : null,
    previewUrl: null,
    downloadUrl: null,
    contentPath: corePath(job.artifact?.content_path),
    downloadPath: corePath(job.artifact?.download_path),
    contentType: job.artifact?.content_type ?? null,
    createdAt: job.created_at,
    durationSeconds: job.artifact?.duration_seconds ?? null,
  }
}

/** A creation known only by its job id (a reopened conversation); the card fetches the rest. */
export function pendingCreation(jobId: string, createdAt: string | null): MediaCreation {
  return { id: jobId, kind: 'image', status: 'queued', progress: null, previewUrl: null, downloadUrl: null, contentType: null, createdAt }
}

/** Newer updates of the same creation replace older ones; order of first appearance is kept. */
export function upsertMedia(list: MediaCreation[] | undefined, media: MediaCreation): MediaCreation[] {
  const current = list ?? []
  const index = current.findIndex((item) => item.id === media.id)
  if (index === -1) return [...current, media]
  return current.map((item, position) => (position === index ? media : item))
}
