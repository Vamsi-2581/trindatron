import { listAgentDefinitions, listAgentInstances, listExecutionJobs } from '@/services/api/agents'
import * as auth from '@/services/api/auth'
import { listOrders, listPaymentProviders, listPlanPrices } from '@/services/api/billing'
import { apiRequest, buildUrl, configureApiClient } from '@/services/api/client'
import { ApiError } from '@/services/api/errors'
import { createProject, deleteProjectFile, getProject, listProjects } from '@/services/api/projects'
import { createSession, deleteMessagesFrom, getSession, listSessions, streamMessage } from '@/services/api/sessions'
import type { ProjectFile } from '@/services/api/types'
import { getCapacity, getEntitlement } from '@/services/api/usage'
import { synthesizeSpeech } from '@/services/api/voice'
import { createId } from '@/utils/ids'

const CORE_URL = process.env.CORE_INTEGRATION_URL
const suite = CORE_URL ? describe : describe.skip

suite('TRINITY CORE contract (real server)', () => {
  let token: string | null = null
  const onUnauthorized = jest.fn()

  beforeAll(async () => {
    configureApiClient({ baseUrl: CORE_URL!.replace(/\/+$/, ''), getToken: () => token, onUnauthorized })
    const email = `mobile-it-${Date.now()}@example.com`
    const registered = await auth.register(email, 'mobile-integration-pass', 'Mobile Integration')
    expect(registered.email).toBe(email)
    token = registered.access_token
  })

  it('authenticates: login, /auth/me, timezone, and a bad credential', async () => {
    const me = await auth.getMe()
    const again = await auth.login(me.email, 'mobile-integration-pass')
    expect(again.user_id).toBe(me.id)
    expect(typeof me.plan).toBe('string')
    expect((await auth.updateTimezone('America/Sao_Paulo')).timezone).toBe('America/Sao_Paulo')

    await expect(auth.login(me.email, 'wrong-password')).rejects.toMatchObject({ kind: 'unauthorized' })
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('treats an invalid token as an expired session', async () => {
    const saved = token
    token = 'not-a-real-token'
    await expect(auth.getMe()).rejects.toMatchObject({ kind: 'unauthorized', status: 401 })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    token = saved
    onUnauthorized.mockClear()
  })

  it('streams a chat turn over SSE and reconciles a failed turn the way retry does', async () => {
    const session = await createSession()
    expect((await listSessions()).sessions.some((item) => item.id === session.id)).toBe(true)

    const statuses: string[] = []
    let text = ''
    const outcome = await streamMessage(
      session.id,
      'Reply with the single word: pong',
      { onStatus: (_key, message) => statuses.push(message), onText: (chunk) => (text += chunk) },
      { idempotencyKey: createId() },
    )

    // With provider keys the Core answers; without them it reports an in-stream error.
    // Either way the protocol must terminate cleanly with a typed outcome.
    expect(['done', 'error']).toContain(outcome.kind)
    expect(statuses.length).toBeGreaterThan(0)

    const detail = await getSession(session.id)
    const last = detail.messages[detail.messages.length - 1]
    if (outcome.kind === 'done') {
      expect(text.length).toBeGreaterThan(0)
      expect(detail.messages.some((message) => message.id === outcome.event.message_id)).toBe(true)
    } else {
      expect(last?.role).toBe('user')
      await deleteMessagesFrom(session.id, last!.id)
      expect((await getSession(session.id)).messages).toHaveLength(0)
    }
  })

  it('manages projects and project files', async () => {
    const project = await createProject({ name: 'Mobile integration', description: 'created by the mobile suite' })
    expect((await listProjects()).projects.some((item) => item.id === project.id)).toBe(true)

    // XMLHttpRequest does not exist in Node; the multipart contract is exercised with fetch here
    // and the XHR transport itself is unit-tested in upload.test.ts.
    const form = new FormData()
    form.append('file', new Blob(['hello from mobile'], { type: 'text/plain' }), 'note.txt')
    const response = await fetch(buildUrl(`/projects/${project.id}/files`), {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    })
    expect(response.status).toBe(201)
    const file = (await response.json()) as ProjectFile
    expect(file).toMatchObject({ name: 'note.txt', mime_type: 'text/plain', size_bytes: 17 })

    expect((await getProject(project.id)).files.map((item) => item.id)).toContain(file.id)
    await deleteProjectFile(project.id, file.id)
    expect((await getProject(project.id)).files).toHaveLength(0)
  })

  it('reads usage without TCU and the billing catalog', async () => {
    const capacity = await getCapacity()
    expect(typeof capacity.capacity_remaining_pct).toBe('number')
    expect(JSON.stringify(capacity)).not.toMatch(/tcu|token|cost/i)

    const entitlement = await getEntitlement()
    expect(entitlement.features).toHaveProperty('supports_agent_mode')

    const plans = await listPlanPrices()
    expect(plans.length).toBeGreaterThan(0)
    expect(plans[0]).toEqual(expect.objectContaining({ plan: expect.any(String), amount: expect.any(String) }))
    expect(Array.isArray((await listPaymentProviders()).providers)).toBe(true)
    expect(Array.isArray((await listOrders({ limit: 5 })).orders)).toBe(true)
  })

  it('lists agents, persistent instances and execution jobs', async () => {
    expect(Array.isArray((await listAgentDefinitions()).agents)).toBe(true)
    expect(Array.isArray((await listAgentInstances()).instances)).toBe(true)
    expect(Array.isArray((await listExecutionJobs()).jobs)).toBe(true)
  })

  it('maps structured Core errors (404, 422, voice provider)', async () => {
    await expect(getSession('00000000-0000-0000-0000-000000000000')).rejects.toMatchObject({ kind: 'not_found' })
    await expect(apiRequest('/projects', { method: 'POST', body: { name: '' } })).rejects.toMatchObject({ kind: 'validation' })

    try {
      const audio = await synthesizeSpeech('teste')
      expect(audio.audio.byteLength).toBeGreaterThan(0)
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      expect(['dependency_unavailable', 'server', 'quota_exceeded']).toContain((error as ApiError).kind)
    }
  })
})
