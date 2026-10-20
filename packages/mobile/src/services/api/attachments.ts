import { apiRequest } from './client'
import { uploadMultipart, type UploadOptions, type UploadPart } from './upload'

/** A file attached to one conversation. Readable documents enter its context on the next message. */
export interface SessionAttachment {
  id: string
  session_id: string
  name: string
  mime_type: string
  size_bytes: number
  kind: string
  project_file_id: string | null
  readable: boolean
  created_at: string
  content_path: string
}

const base = (sessionId: string) => `/sessions/${encodeURIComponent(sessionId)}/attachments`

export function listAttachments(sessionId: string, signal?: AbortSignal): Promise<{ attachments: SessionAttachment[] }> {
  return apiRequest<{ attachments: SessionAttachment[] }>(base(sessionId), { signal })
}

export function attachUpload(sessionId: string, part: UploadPart, options: UploadOptions = {}): Promise<SessionAttachment> {
  return uploadMultipart<SessionAttachment>(base(sessionId), part, options)
}

export function attachProjectFile(sessionId: string, projectFileId: string): Promise<SessionAttachment> {
  return apiRequest<SessionAttachment>(`${base(sessionId)}/project-file`, { method: 'POST', body: { project_file_id: projectFileId } })
}

export function removeAttachment(sessionId: string, attachmentId: string): Promise<void> {
  return apiRequest<void>(`${base(sessionId)}/${encodeURIComponent(attachmentId)}`, { method: 'DELETE' })
}
