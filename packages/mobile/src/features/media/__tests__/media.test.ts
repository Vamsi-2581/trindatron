import { chatReducer, initialChatState, type ChatState } from '@/features/chat/chatReducer'

import type { MediaJobView } from '@/services/api/media'

import { creationFromJob, parseMediaArtifact, safeMediaUrl, upsertMedia } from '../media'

const job: MediaJobView = {
  id: 'j1',
  kind: 'video',
  operation: 'generate',
  status: 'completed',
  progress: 1,
  error_code: null,
  project_id: null,
  created_at: '2026-09-14T12:00:00Z',
  completed_at: '2026-09-14T12:04:00Z',
  artifact: {
    id: 'a1',
    kind: 'video',
    content_type: 'video/mp4',
    size_bytes: 1024,
    width: 1280,
    height: 720,
    duration_seconds: 5,
    created_at: '2026-09-14T12:04:00Z',
    expires_at: null,
    content_path: '/media/artifacts/a1/content',
    download_path: '/media/artifacts/a1/download',
  },
}

const artifact = {
  id: 'm1',
  kind: 'image',
  status: 'completed',
  progress: 1,
  preview_url: 'https://cdn.example.com/signed/m1.png',
  download_url: 'https://cdn.example.com/signed/m1.png?download=1',
  content_type: 'image/png',
  created_at: '2026-09-14T12:00:00Z',
  provider: 'some-vendor',
  model: 'some-model',
  cost: 0.01,
}

describe('media creations', () => {
  it('reads only the fields the app shows — never engine, model or cost', () => {
    const media = parseMediaArtifact(artifact)
    expect(media).toEqual({
      id: 'm1',
      kind: 'image',
      status: 'completed',
      progress: 1,
      previewUrl: artifact.preview_url,
      downloadUrl: artifact.download_url,
      contentType: 'image/png',
      createdAt: '2026-09-14T12:00:00Z',
    })
    expect(JSON.stringify(media)).not.toMatch(/some-vendor|some-model|cost/)
  })

  it('rejects unknown kinds and states, and links that are not signed HTTPS', () => {
    expect(parseMediaArtifact({ ...artifact, kind: 'hologram' })).toBeNull()
    expect(parseMediaArtifact({ ...artifact, status: 'maybe' })).toBeNull()
    expect(parseMediaArtifact({ ...artifact, id: '' })).toBeNull()
    expect(safeMediaUrl('javascript:alert(1)', true)).toBeNull()
    expect(safeMediaUrl('http://cdn.example.com/x.png', false)).toBeNull()
    expect(safeMediaUrl('https://cdn.example.com/x.png', false)).toBe('https://cdn.example.com/x.png')
    expect(parseMediaArtifact({ ...artifact, progress: 7 })?.progress).toBe(1)
  })

  it('turns a Core media job into a card, reading the file only through a Core media path', () => {
    const extra = { ...job, engine: 'some-engine', cost_usd: 0.4 } as MediaJobView
    const media = creationFromJob(extra)
    expect(media).toMatchObject({
      id: 'j1',
      kind: 'video',
      status: 'completed',
      contentPath: '/media/artifacts/a1/content',
      downloadPath: '/media/artifacts/a1/download',
      durationSeconds: 5,
    })
    expect(JSON.stringify(media)).not.toMatch(/some-engine|cost/)

    const foreign = creationFromJob({
      ...job,
      artifact: { ...job.artifact!, content_path: 'https://elsewhere.example.com/x', download_path: 'https://elsewhere.example.com/x?dl=1' },
    })
    expect(foreign.contentPath).toBeNull()
    expect(foreign.downloadPath).toBeNull()
    expect(creationFromJob({ ...job, status: 'running', progress: 3, artifact: null })).toMatchObject({ status: 'running', progress: 1, contentPath: null, downloadPath: null })
  })

  it('replaces progress updates of the same creation in place', () => {
    const running = parseMediaArtifact({ ...artifact, status: 'running', progress: 0.3, preview_url: null })!
    const done = parseMediaArtifact(artifact)!
    const other = parseMediaArtifact({ ...artifact, id: 'm2' })!
    expect(upsertMedia(upsertMedia(upsertMedia(undefined, running), other), done)).toEqual([done, other])
  })

  it('attaches creations to the answer being streamed, and keeps an answer that is only media', () => {
    let state: ChatState = { ...initialChatState, phase: 'ready' }
    state = chatReducer(state, { type: 'turnStarted', content: 'Crie uma imagem de um farol', turnId: 't', now: '2026-09-14T12:00:00Z' })
    state = chatReducer(state, { type: 'media', media: parseMediaArtifact({ ...artifact, status: 'running', progress: 0.5 })! })
    state = chatReducer(state, { type: 'media', media: parseMediaArtifact(artifact)! })
    state = chatReducer(state, { type: 'turnDone', event: { type: 'done', session_id: 's', message_id: 'a1', user_message_id: 'u1' } })

    const answer = state.messages.find((message) => message.role === 'assistant')
    expect(answer).toMatchObject({ id: 'a1', content: '', state: 'final' })
    expect(answer?.media).toHaveLength(1)
    expect(answer?.media?.[0]?.status).toBe('completed')
  })
})
