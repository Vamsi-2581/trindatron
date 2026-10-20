import type { Project, ProjectFile } from '@/services/api/types'

export type FileKind = 'all' | 'images' | 'documents'

export interface LibraryFile extends ProjectFile {
  projectName: string
}

export interface LibraryFilter {
  query?: string
  kind?: FileKind
  projectId?: string | null
}

export function fileKind(mimeType: string): Exclude<FileKind, 'all'> {
  return mimeType.startsWith('image/') ? 'images' : 'documents'
}

/**
 * The user's whole file library, newest first, each file tagged with the
 * project it belongs to. Built from GET /projects — the Core's only file
 * listing today (a dedicated library endpoint is documented as a Core gap).
 */
export function buildFileLibrary(projects: Project[], filter: LibraryFilter = {}): LibraryFile[] {
  const query = filter.query?.trim().toLocaleLowerCase() ?? ''
  const kind = filter.kind ?? 'all'
  return projects
    .filter((project) => !filter.projectId || project.id === filter.projectId)
    .flatMap((project) => project.files.map((file) => ({ ...file, projectName: project.name })))
    .filter((file) => kind === 'all' || fileKind(file.mime_type) === kind)
    .filter((file) => !query || file.name.toLocaleLowerCase().includes(query) || file.projectName.toLocaleLowerCase().includes(query))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
}
