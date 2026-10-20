import { apiRequest } from './client'
import { uploadMultipart, type UploadOptions, type UploadPart } from './upload'
import type { Project, ProjectFile, ProjectList, SessionList } from './types'

export function listProjects(signal?: AbortSignal): Promise<ProjectList> {
  return apiRequest<ProjectList>('/projects', { signal })
}

export function getProject(projectId: string, signal?: AbortSignal): Promise<Project> {
  return apiRequest<Project>(`/projects/${encodeURIComponent(projectId)}`, { signal })
}

export function createProject(input: { name: string; description?: string | null; instructions?: string | null }): Promise<Project> {
  return apiRequest<Project>('/projects', {
    method: 'POST',
    body: { name: input.name, description: input.description ?? null, instructions: input.instructions ?? null },
  })
}

export function updateProject(
  projectId: string,
  updates: { name?: string; description?: string | null; instructions?: string | null },
): Promise<Project> {
  return apiRequest<Project>(`/projects/${encodeURIComponent(projectId)}`, { method: 'PATCH', body: updates })
}

export function archiveProject(projectId: string): Promise<void> {
  return apiRequest<void>(`/projects/${encodeURIComponent(projectId)}`, { method: 'DELETE' })
}

export function listProjectSessions(projectId: string, signal?: AbortSignal): Promise<SessionList> {
  return apiRequest<SessionList>(`/projects/${encodeURIComponent(projectId)}/sessions`, { signal })
}

export function uploadProjectFile(projectId: string, part: UploadPart, options?: UploadOptions): Promise<ProjectFile> {
  return uploadMultipart<ProjectFile>(`/projects/${encodeURIComponent(projectId)}/files`, part, options)
}

export function deleteProjectFile(projectId: string, fileId: string): Promise<void> {
  return apiRequest<void>(
    `/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(fileId)}`,
    { method: 'DELETE' },
  )
}
