import type { Project } from '@/services/api/types'

import { buildFileLibrary, fileKind } from '../library'

function project(id: string, name: string, files: { id: string; name: string; mime: string; created: string }[]): Project {
  return {
    id,
    user_id: 'u1',
    name,
    description: null,
    instructions: null,
    is_archived: false,
    created_at: '2026-09-01T10:00:00',
    updated_at: '2026-09-01T10:00:00',
    files: files.map((file) => ({
      id: file.id,
      project_id: id,
      name: file.name,
      mime_type: file.mime,
      size_bytes: 10,
      created_at: file.created,
    })),
  }
}

const projects = [
  project('p1', 'Lançamento', [
    { id: 'f1', name: 'briefing.pdf', mime: 'application/pdf', created: '2026-09-10T10:00:00' },
    { id: 'f2', name: 'foto-palco.jpg', mime: 'image/jpeg', created: '2026-09-12T10:00:00' },
  ]),
  project('p2', 'Pesquisa', [{ id: 'f3', name: 'entrevistas.docx', mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', created: '2026-09-11T10:00:00' }]),
]

describe('file library', () => {
  it('lists every file across projects, newest first, tagged with its project', () => {
    expect(buildFileLibrary(projects).map((file) => [file.id, file.projectName])).toEqual([
      ['f2', 'Lançamento'],
      ['f3', 'Pesquisa'],
      ['f1', 'Lançamento'],
    ])
  })

  it('filters by type, project and search text', () => {
    expect(buildFileLibrary(projects, { kind: 'images' }).map((file) => file.id)).toEqual(['f2'])
    expect(buildFileLibrary(projects, { kind: 'documents' }).map((file) => file.id)).toEqual(['f3', 'f1'])
    expect(buildFileLibrary(projects, { projectId: 'p2' }).map((file) => file.id)).toEqual(['f3'])
    expect(buildFileLibrary(projects, { query: 'BRIEF' }).map((file) => file.id)).toEqual(['f1'])
    expect(buildFileLibrary(projects, { query: 'pesquisa' }).map((file) => file.id)).toEqual(['f3'])
    expect(buildFileLibrary(projects, { query: 'nada' })).toEqual([])
  })

  it('classifies files by MIME type', () => {
    expect(fileKind('image/png')).toBe('images')
    expect(fileKind('application/pdf')).toBe('documents')
  })
})
