import type { Entitlement } from '@/services/api/types'
import * as presentationsApi from '@/services/api/presentations'
import * as usageApi from '@/services/api/usage'

import {
  availableCreations,
  baselineCapabilities,
  capabilitiesFromEntitlement,
  MEDIA_CAPABILITIES,
  parseCoreCapabilities,
  PRODUCT_CAPABILITIES,
  statusOf,
} from '../capabilities'
import { useCapabilityStore } from '../store'

jest.mock('@/services/api/usage')
jest.mock('@/services/api/presentations')

const usage = usageApi as jest.Mocked<typeof usageApi>
const presentations = presentationsApi as jest.Mocked<typeof presentationsApi>

function entitlement(overrides: Partial<Entitlement['features']> = {}, maxAgents: number | null = 0): Entitlement {
  return {
    plan: 'free',
    quota: { daily_tcu_limit: 10, weekly_tcu_limit: 20, monthly_tcu_limit: null, max_tcu_per_request: 2 },
    features: {
      supports_agent_mode: false,
      supports_trinity_code: false,
      supports_trinity_work: false,
      supports_image_generation: false,
      ...overrides,
    },
    limits: { max_projects: 3, max_agents: maxAgents },
  }
}

const PROVIDER_NAMES = /(gpt|openai|anthropic|claude|gemini|google|mistral|grok|xai|runway|kling|veo|flux|wan|ltx|hunyuan|comfy)/i

describe('capability discovery', () => {
  it('names capabilities abstractly — never an engine or vendor', () => {
    for (const id of [...MEDIA_CAPABILITIES, ...PRODUCT_CAPABILITIES]) expect(id).not.toMatch(PROVIDER_NAMES)
  })

  it('starts with only what every user has: voice and project files, no media', () => {
    const base = baselineCapabilities()
    expect(base.voice_input.available).toBe(true)
    expect(base.project_files.available).toBe(true)
    for (const id of MEDIA_CAPABILITIES) expect(base[id]).toMatchObject({ available: false, dailyQuota: null, limits: null })
    expect(base.chat_attachments.available).toBe(false)
    expect(base.browser_control.available).toBe(false)
    expect(base.computer_control.available).toBe(false)
    expect(base.audio_output.available).toBe(false)
  })

  it('derives plan features from the Core entitlement, never invents a quota, and never offers media from a plan flag', () => {
    const free = capabilitiesFromEntitlement(entitlement(), { presentations: false })
    expect(free.agent_mode.available).toBe(false)
    expect(free.persistent_agents.available).toBe(false)
    expect(free.image_generation).toMatchObject({ available: false, dailyQuota: null, limits: null })

    const paid = capabilitiesFromEntitlement(entitlement({ supports_agent_mode: true, supports_image_generation: true }, 5), { presentations: true })
    expect(paid.agent_mode.available).toBe(true)
    expect(paid.persistent_agents.available).toBe(true)
    expect(paid.presentations.available).toBe(true)
    // Only the Core's capability answer can say an engine is ready; the plan flag alone cannot.
    expect(paid.image_generation).toMatchObject({ available: false, dailyQuota: null, limits: null })
    expect(paid.video_generation.available).toBe(false)
  })

  it('reads daily quotas and plan limits exactly as the Core sends them, and nothing else', () => {
    const parsed = parseCoreCapabilities({
      capabilities: {
        image_generation: {
          available: true,
          provider: 'some-vendor',
          model: 'some-model',
          cost_usd: 0.004,
          daily_quota: { limit: 10, remaining: 12, resets_at: '2026-09-15T03:00:00Z' },
          limits: { quality_tier: 'standard', max_resolution: '1024x1024' },
        },
        video_generation: { available: true, daily_quota: { limit: 2, remaining: 1 }, limits: { max_duration_seconds: 5, max_resolution: '480p' } },
        avatar_generation: { available: 'yes' },
        unknown_capability: { available: true },
      },
    })
    expect(parsed).not.toBeNull()
    expect(parsed!.image_generation).toMatchObject({
      available: true,
      dailyQuota: { limit: 10, remaining: 10, resetsAt: '2026-09-15T03:00:00Z' },
      limits: { qualityTier: 'standard', maxResolution: '1024x1024', maxDurationSeconds: null },
    })
    expect(JSON.stringify(parsed)).not.toMatch(/some-vendor|some-model|cost/)
    expect(parsed!.video_generation.dailyQuota).toEqual({ limit: 2, remaining: 1, resetsAt: null })
    expect(parsed!.video_generation.limits?.maxDurationSeconds).toBe(5)
    expect(parsed!.avatar_generation.available).toBe(false)
    expect(parseCoreCapabilities({ nope: true })).toBeNull()
  })

  it('keeps the reason a capability is off, instead of one flat "unavailable"', () => {
    const parsed = parseCoreCapabilities({
      capabilities: {},
      matrix: {
        image_generation: { available: false, status: 'unavailable', health: 'down' },
        video_generation: { available: false, status: 'plan' },
        avatar_generation: { available: false, status: 'not_integrated' },
        computer_control: { available: false, status: 'plan', scope: 'sandbox' },
        browser_control: { available: true, status: 'available', scope: 'sandbox' },
        agent: { available: true, status: 'available' },
        audio_output: { available: true, status: 'available' },
        transcription: { available: true, status: 'available' },
        file_analysis: { available: true, status: 'available' },
        document_generation: { available: false, status: 'plan' },
        vision: { available: false, status: 'unavailable' },
      },
    })!

    expect(statusOf(parsed.image_generation)).toBe('unavailable')
    expect(statusOf(parsed.video_generation)).toBe('plan')
    expect(statusOf(parsed.avatar_generation)).toBe('not_integrated')
    // Capabilities that existed only in the matrix were invisible to the app before.
    expect(parsed.browser_control).toMatchObject({ available: true, scope: 'sandbox' })
    expect(statusOf(parsed.computer_control)).toBe('plan')
    expect(parsed.audio_output.available).toBe(true)
    expect(parsed.chat_attachments.available).toBe(true)
    expect(statusOf(parsed.presentations)).toBe('plan')
    expect(parsed.image_understanding.available).toBe(false)
  })

  it('reads an answer with no matrix, and one with no per-feature view', () => {
    const legacyOnly = parseCoreCapabilities({ capabilities: { image_generation: { available: true } } })!
    expect(legacyOnly.image_generation.available).toBe(true)
    expect(statusOf(legacyOnly.image_generation)).toBe('available')

    const matrixOnly = parseCoreCapabilities({ matrix: { agent: { available: true, status: 'available' } } })!
    expect(matrixOnly.agent_mode.available).toBe(true)

    expect(parseCoreCapabilities({ nope: true })).toBeNull()
    expect(parseCoreCapabilities('not an object')).toBeNull()
  })

  it('takes the matrix as the authority when the two views disagree', () => {
    // The per-feature view answers about the plan; only the matrix knows an engine went down.
    const parsed = parseCoreCapabilities({
      capabilities: { image_generation: { available: true, daily_quota: { limit: 5, remaining: 5 } } },
      matrix: { image_generation: { available: false, status: 'unavailable' } },
    })!
    expect(parsed.image_generation.available).toBe(false)
    expect(statusOf(parsed.image_generation)).toBe('unavailable')
    // The allowance is still reported, so the screen can say what the plan gives.
    expect(parsed.image_generation.dailyQuota).toEqual({ limit: 5, remaining: 5, resetsAt: null })
  })

  it('an unknown capability is simply absent, never invented', () => {
    const parsed = parseCoreCapabilities({ matrix: { teleportation: { available: true, status: 'available' } } })!
    expect(Object.keys(parsed)).not.toContain('teleportation')
    expect(statusOf(undefined)).toBe('not_integrated')
  })

  it('a plan that does not include something says so, rather than calling it broken', () => {
    const free = capabilitiesFromEntitlement(entitlement(), { presentations: false })
    expect(statusOf(free.agent_mode)).toBe('plan')
    expect(statusOf(free.persistent_agents)).toBe('plan')
  })

  it('offers creation entry points only for capabilities the Core reports as available', () => {
    const capabilities = baselineCapabilities()
    expect(availableCreations(capabilities)).toEqual([])
    capabilities.video_generation = { available: true, dailyQuota: null, limits: null }
    expect(availableCreations(capabilities)).toEqual(['video_generation'])
  })
})

describe('capability store', () => {
  beforeEach(() => {
    jest.resetAllMocks()
    useCapabilityStore.setState({ capabilities: baselineCapabilities(), loaded: false, userId: null })
  })

  it('asks the Core, not the device', async () => {
    usage.getEntitlement.mockResolvedValue(entitlement({ supports_agent_mode: true }, 5))
    presentations.getPresentationCapability.mockResolvedValue({ available: true, max_slides: 20 })
    await useCapabilityStore.getState().load('u1')
    const state = useCapabilityStore.getState()
    expect(state.loaded).toBe(true)
    expect(state.capabilities.agent_mode.available).toBe(true)
    expect(state.capabilities.presentations.available).toBe(true)
  })

  it('a failed read never turns anything on, and another user starts from scratch', async () => {
    usage.getEntitlement.mockResolvedValue(entitlement({ supports_agent_mode: true }, 5))
    presentations.getPresentationCapability.mockRejectedValue(new Error('404'))
    await useCapabilityStore.getState().load('u1')
    expect(useCapabilityStore.getState().capabilities.presentations.available).toBe(false)

    usage.getEntitlement.mockRejectedValue(new Error('offline'))
    await useCapabilityStore.getState().load('u2')
    expect(useCapabilityStore.getState()).toMatchObject({ loaded: false, userId: 'u2' })
    expect(useCapabilityStore.getState().capabilities.agent_mode.available).toBe(false)
  })
})
