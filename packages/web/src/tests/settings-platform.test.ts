/**
 * Settings, Computer Use and Scheduled are real screens over the Core.
 *
 * Like workspace-nav.test.ts, these read the sources: what is guarded is a
 * decision made while editing (a section that exists only as a card, a label
 * that names a provider, an agent turn that skips the approval gate), not a
 * rendering detail.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { SETTINGS_GROUPS, SETTINGS_SECTION_IDS } from '@/components/settings/nav'

const root = join(__dirname, '..')
const read = (...parts: string[]) => readFileSync(join(root, ...parts), 'utf-8')

const sections = read('components', 'settings', 'sections.tsx')
const sidebar = read('components', 'layout', 'sidebar.tsx')
const chat = read('app', '(app)', 'chat', '[sessionId]', 'page.tsx')
const computerUse = read('app', '(app)', 'computer-use', 'page.tsx')
const scheduled = read('components', 'scheduled', 'scheduled-tasks.tsx')

const PROVIDERS = /openai|anthropic|claude|gemini|mistral|groq|\bxai\b|grok|litellm|deepseek|\bTCU\b|model_id|provider_cost/i

describe('settings map', () => {
  it('groups every section the product defines', () => {
    expect(SETTINGS_GROUPS.map((g) => g.label)).toEqual(['Personalizar', 'Configurações', 'Aplicativo'])
    for (const id of ['skills', 'connectors', 'plugins', 'general', 'account', 'privacy', 'billing', 'usage', 'capabilities',
      'memory', 'computer-use', 'scheduled', 'work', 'voice', 'media', 'desktop', 'extensions']) {
      expect(SETTINGS_SECTION_IDS).toContain(id)
    }
  })

  it('renders a component for every section, and no section is a "coming soon" card', () => {
    const block = sections.slice(sections.indexOf('export const SECTION_COMPONENTS'))
    for (const id of SETTINGS_SECTION_IDS) {
      expect(block).toMatch(new RegExp(`['"]?${id}['"]?:\\s*\\w+Section`))
    }
    expect(sections).not.toMatch(/EM BREVE/)
  })

  it('reads its state from the Core, never from hard-coded availability', () => {
    for (const fn of ['apiGetPreferences', 'apiUpdatePreferences', 'apiGetIntegrations', 'apiSetPluginEnabled',
      'apiGetCapabilityMatrix', 'apiListDeviceSessions', 'apiSignOutEverywhere', 'apiExportMyData', 'apiClearMemories',
      'apiListMySubscriptions', 'apiCancelSubscription', 'apiGetCapacity', 'apiListExecutionJobs']) {
      expect(sections).toContain(fn)
    }
  })

  it('names no provider, model or internal unit anywhere a person reads', () => {
    for (const source of [sections, computerUse, scheduled]) {
      expect(source).not.toMatch(PROVIDERS)
    }
  })
})

describe('preferences have an effect', () => {
  const composer = read('components', 'chat', 'composer.tsx')
  const bubble = read('components', 'chat', 'message-bubble.tsx')

  it('offers only preferences something honours', () => {
    const client = read('api', 'client.ts')
    const block = client.slice(client.indexOf('export interface Preferences'), client.indexOf('export async function apiGetPreferences'))
    expect(block).not.toMatch(/theme|voice_input_language/)
  })

  it('send_on_enter decides what Enter does in the composer', () => {
    expect(composer).toContain('preferences.send_on_enter')
    expect(composer).toMatch(/if \(sendOnEnter \|\| modifier\)/)
  })

  it('voice_auto_play reads a finished answer with the same read-aloud as the button, only when voice output exists', () => {
    expect(chat).toContain('preferences.voice_auto_play && capabilities.audio_output?.available === true')
    expect(chat).toContain('autoRead={msg.id === autoReadId}')
    expect(bubble).toMatch(/void handlePlayAudio\(\)/)
  })
})

describe('navigation', () => {
  it('makes Computer Use, Scheduled and Plugins easy to find', () => {
    for (const href of ['/computer-use', '/scheduled', '/settings/plugins']) {
      expect(sidebar).toContain(`href: '${href}'`)
    }
  })
})

describe('Computer Use', () => {
  it('promises only what the Core does: an isolated environment and approval before changes', () => {
    expect(computerUse).toContain('Deixe o TRINDATRON usar seu computador por você.')
    expect(computerUse).toContain('Você não precisa saber onde clicar. Você só precisa saber o que quer fazer.')
    expect(computerUse).toMatch(/isolados, não no seu próprio dispositivo/)
    expect(computerUse).toMatch(/pede a sua aprovação/)
    expect(computerUse).toContain('apiGetCapabilityMatrix')
  })

  it('runs its conversation through the agent and keeps the approval gate', () => {
    expect(chat).toMatch(/mode'\) === 'agent'/)
    expect(chat).toContain('apiStreamAgent(')
    expect(chat).toContain('apiApproveAgentAction')
    expect(chat).toContain('apiRejectAgentAction')
  })
})
