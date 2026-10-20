import { configureApiClient } from '../client'
import { uploadMultipart } from '../upload'

class FakeFormData {
  parts: [string, unknown][] = []
  append(name: string, value: unknown) {
    this.parts.push([name, value])
  }
}

class FakeXHR {
  static instances: FakeXHR[] = []
  upload: { onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null }
  status = 0
  responseText = ''
  timeout = 0
  headers: Record<string, string> = {}
  method = ''
  url = ''
  body: unknown
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  ontimeout: (() => void) | null = null
  onabort: (() => void) | null = null
  constructor() {
    FakeXHR.instances.push(this)
  }
  open(method: string, url: string) {
    this.method = method
    this.url = url
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value
  }
  send(body: unknown) {
    this.body = body
  }
  abort() {
    this.onabort?.()
  }
  respond(status: number, body: unknown) {
    this.status = status
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
const onUnauthorized = jest.fn()
const part = { uri: 'file:///tmp/photo.jpg', name: 'photo.jpg', mimeType: 'image/jpeg' }

beforeEach(() => {
  FakeXHR.instances = []
  onUnauthorized.mockReset()
  Object.assign(globalThis, { XMLHttpRequest: FakeXHR, FormData: FakeFormData })
  configureApiClient({ baseUrl: 'https://core.test', getToken: () => 'tok', onUnauthorized })
})

describe('uploadMultipart', () => {
  it('streams the file by URI with auth, reports progress and resolves the created file', async () => {
    const progress: number[] = []
    const pending = uploadMultipart('/projects/p1/files', part, { onProgress: (value) => progress.push(value) })
    await tick()
    const xhr = FakeXHR.instances[0]!

    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('https://core.test/projects/p1/files')
    expect(xhr.headers.Authorization).toBe('Bearer tok')
    expect((xhr.body as FakeFormData).parts).toEqual([['file', { uri: part.uri, name: 'photo.jpg', type: 'image/jpeg' }]])

    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 })
    xhr.respond(201, { id: 'f1', name: 'photo.jpg' })

    await expect(pending).resolves.toEqual({ id: 'f1', name: 'photo.jpg' })
    expect(progress).toEqual([0.5, 1])
  })

  it('maps a 413 from the Core', async () => {
    const pending = uploadMultipart('/projects/p1/files', part)
    await tick()
    FakeXHR.instances[0]!.respond(413, { detail: 'File exceeds 10 MB limit' })
    await expect(pending).rejects.toMatchObject({ kind: 'payload_too_large', message: 'O arquivo é maior do que o limite permitido.' })
  })

  it('ends the session on 401', async () => {
    const pending = uploadMultipart('/projects/p1/files', part)
    await tick()
    FakeXHR.instances[0]!.respond(401, { detail: 'Invalid or expired token' })
    await expect(pending).rejects.toMatchObject({ kind: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalled()
  })

  it('cancels through the abort signal', async () => {
    const controller = new AbortController()
    const pending = uploadMultipart('/projects/p1/files', part, { signal: controller.signal })
    await tick()
    controller.abort()
    await expect(pending).rejects.toMatchObject({ kind: 'aborted' })
  })

  it('reports network failures and timeouts', async () => {
    const failing = uploadMultipart('/projects/p1/files', part)
    const slow = uploadMultipart('/projects/p1/files', part)
    await tick()
    FakeXHR.instances[0]!.onerror?.()
    FakeXHR.instances[1]!.ontimeout?.()
    await expect(failing).rejects.toMatchObject({ kind: 'network' })
    await expect(slow).rejects.toMatchObject({ kind: 'timeout' })
  })
})
