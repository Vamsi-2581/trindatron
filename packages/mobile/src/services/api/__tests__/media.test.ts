import { apiRequest } from '../client'
import { createImage, createVideo, deleteCreation, getMediaJob, listCreations } from '../media'

jest.mock('../client', () => ({
  apiRequest: jest.fn(),
  authHeaders: jest.fn(() => ({ Authorization: 'Bearer t' })),
  buildUrl: jest.fn((path: string) => `https://core.example.com${path}`),
}))

const request = apiRequest as jest.Mock

describe('media API', () => {
  beforeEach(() => request.mockReset().mockResolvedValue({}))

  it('creates images and videos as Core jobs with an idempotency key and a free-form format', async () => {
    await createImage({ prompt: 'um farol', format: '1080x1920' }, 'k1')
    expect(request).toHaveBeenCalledWith('/media/images', {
      method: 'POST',
      headers: { 'Idempotency-Key': 'k1' },
      body: { prompt: 'um farol', format: '1080x1920', project_id: null },
    })

    await createVideo({ prompt: 'ondas', format: 'Instagram Story', durationSeconds: 5 }, 'k2')
    expect(request).toHaveBeenLastCalledWith('/media/videos', {
      method: 'POST',
      headers: { 'Idempotency-Key': 'k2' },
      body: { prompt: 'ondas', format: 'Instagram Story', project_id: null, duration_seconds: 5 },
    })
  })

  it('leaves the format to the description when none is given', async () => {
    await createImage({ prompt: 'um farol vertical' }, 'k3')
    expect(request).toHaveBeenLastCalledWith('/media/images', {
      method: 'POST',
      headers: { 'Idempotency-Key': 'k3' },
      body: { prompt: 'um farol vertical', format: null, project_id: null },
    })
  })

  it('follows jobs and pages the gallery with the Core cursor', async () => {
    await getMediaJob('j/1')
    expect(request).toHaveBeenCalledWith('/media/jobs/j%2F1', { signal: undefined })

    await listCreations({ kind: 'video', before: 'c9', limit: 10 })
    expect(request).toHaveBeenLastCalledWith('/media/creations?kind=video&before=c9&limit=10', { signal: undefined })

    await deleteCreation('j1')
    expect(request).toHaveBeenLastCalledWith('/media/creations/j1', { method: 'DELETE' })
  })
})
