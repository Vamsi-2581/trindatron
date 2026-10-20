import fs from 'node:fs'
import path from 'node:path'

import { render, screen } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'

import ComputerUseScreen from '@/../app/(app)/computer-use'
import CreateScreen from '@/../app/(app)/create'
import ScheduledScreen from '@/../app/(app)/scheduled'
import { baselineCapabilities, type Capabilities } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import { useLocaleStore } from '@/i18n'
import en from '@/i18n/locales/en.json'
import es from '@/i18n/locales/es.json'
import pt from '@/i18n/locales/pt.json'
import * as scheduledApi from '@/services/api/scheduled'

jest.mock('expo-web-browser', () => ({ maybeCompleteAuthSession: jest.fn() }))
jest.mock('@/services/api/scheduled')

const scheduled = scheduledApi as jest.Mocked<typeof scheduledApi>

beforeEach(() => {
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
})

const ROOT = path.resolve(__dirname, '../..')

function withStatus(ids: keyof Capabilities | (keyof Capabilities)[], status: 'available' | 'plan' | 'unavailable' | 'not_integrated'): Capabilities {
  const capabilities = baselineCapabilities()
  for (const id of Array.isArray(ids) ? ids : [ids]) capabilities[id] = { available: status === 'available', status, dailyQuota: null, limits: null }
  return capabilities
}

const COMPUTER_USE: (keyof Capabilities)[] = ['browser_control', 'computer_control']

describe('Computer Use entry', () => {
  it('invites the person when the Core says the browser is reachable', () => {
    useCapabilityStore.setState({ capabilities: withStatus('browser_control', 'available'), loaded: true })
    render(<ComputerUseScreen />)

    expect(screen.getByText(pt.computerUse.headline)).toBeTruthy()
    // The promise is bounded: an isolated browser, and approval before anything changes.
    expect(screen.getByText(pt.computerUse.isolationNote)).toBeTruthy()
    expect(screen.getByText(pt.computerUse.approvalNote)).toBeTruthy()
  })

  it('tells the plan, this environment and "not in the product" apart — and promises nothing', () => {
    useCapabilityStore.setState({ capabilities: withStatus(COMPUTER_USE, 'plan'), loaded: true })
    const { unmount } = render(<ComputerUseScreen />)
    expect(screen.getByText(pt.computerUse.state.plan.title)).toBeTruthy()
    expect(screen.getByText(pt.computerUse.state.plan.body)).toBeTruthy()
    expect(screen.getByRole('button', { name: pt.usage.seePlans })).toBeTruthy()
    unmount()

    // Included in the plan, but not running where the app is connected (a development machine without its sandbox).
    useCapabilityStore.setState({ capabilities: withStatus(COMPUTER_USE, 'unavailable'), loaded: true })
    const second = render(<ComputerUseScreen />)
    expect(screen.getByText(pt.computerUse.state.unavailable.title)).toBeTruthy()
    expect(screen.getByText(pt.computerUse.state.unavailable.body)).toBeTruthy()
    expect(screen.queryByRole('button', { name: pt.usage.seePlans })).toBeNull()
    expect(screen.queryByText(/instantes|Ainda não/)).toBeNull()
    second.unmount()

    useCapabilityStore.setState({ capabilities: withStatus(COMPUTER_USE, 'not_integrated'), loaded: true })
    render(<ComputerUseScreen />)
    expect(screen.getByText(pt.computerUse.state.not_integrated.body)).toBeTruthy()
  })

  it('waits for the Core before saying anything is off', () => {
    // Before the matrix arrives the baseline says "unavailable": showing that would be a false outage.
    useCapabilityStore.setState({ capabilities: withStatus(COMPUTER_USE, 'unavailable'), loaded: false })
    render(<ComputerUseScreen />)
    expect(screen.queryByText(pt.computerUse.state.unavailable.body)).toBeNull()
    expect(screen.getByRole('progressbar')).toBeTruthy()
  })

  it('never offers to drive the phone itself', () => {
    const source = fs.readFileSync(path.join(ROOT, 'app', '(app)', 'computer-use.tsx'), 'utf8')
    // The Core executes; this screen only asks. A local executor here would be a lie about where it runs.
    expect(source).not.toMatch(/desktop_click|desktop_type|executeShell|localExecutor/)
  })
})

describe('Create entry', () => {
  it('says the real reason it cannot create, and offers plans only when the plan is the reason', async () => {
    useCapabilityStore.setState({ capabilities: withStatus('image_generation', 'plan'), loaded: true })
    const { unmount } = renderRouter({ create: CreateScreen, billing: () => null }, { initialUrl: '/create' })

    expect(await screen.findByText(pt.create.planBody)).toBeTruthy()
    expect(screen.getByText(pt.tips.seePlans)).toBeTruthy()
    unmount()

    useCapabilityStore.setState({ capabilities: withStatus('image_generation', 'unavailable'), loaded: true })
    renderRouter({ create: CreateScreen, billing: () => null }, { initialUrl: '/create' })

    expect(await screen.findByText(pt.create.unavailableBody)).toBeTruthy()
    // An outage is not something to buy your way out of, so no upgrade is offered here.
    expect(screen.queryByText(pt.tips.seePlans)).toBeNull()
  })
})

describe('navigation', () => {
  // The side menu lists its tools from one table.
  const panel = fs.readFileSync(path.join(ROOT, 'src', 'features', 'navigation', 'tools.ts'), 'utf8')
  const routes = fs.readFileSync(path.join(ROOT, 'src', 'features', 'navigation', 'routes.ts'), 'utf8')

  it('offers the new places in the side menu', () => {
    expect(panel).toContain('SCHEDULED_HREF')
    expect(panel).toContain('COMPUTER_USE_HREF')
  })

  it('every declared route has a screen behind it', () => {
    const hrefs = [...routes.matchAll(/'(\/[a-z-/]+)' as Href/g)].map((match) => match[1]!)
    expect(hrefs.length).toBeGreaterThanOrEqual(6)
    for (const href of hrefs) {
      const base = path.join(ROOT, 'app', '(app)', href.replace(/^\//, ''))
      const exists = fs.existsSync(`${base}.tsx`) || fs.existsSync(path.join(base, 'index.tsx'))
      expect(exists).toBe(true)
    }
  })

  it('opens Settings from the profile', () => {
    const profile = fs.readFileSync(path.join(ROOT, 'app', '(app)', '(tabs)', 'profile.tsx'), 'utf8')
    expect(profile).toContain('SETTINGS_HREF')
  })
})

describe('public naming', () => {
  it('names what the product does, never an internal component', () => {
    for (const dictionary of [pt, en, es]) {
      const text = JSON.stringify(dictionary)
      expect(text).not.toMatch(/TRINITY CODE|TRINITY WORK/)
    }
  })

  it('still names the product itself', () => {
    expect(JSON.stringify(pt)).toMatch(/TRINDATRON/)
  })
})

describe('settings screens exist for the contracts the Core offers', () => {
  it('has a screen for preferences, devices, memories and privacy', () => {
    for (const file of ['index.tsx', 'devices.tsx', 'memories.tsx', 'privacy.tsx']) {
      expect(fs.existsSync(path.join(ROOT, 'app', '(app)', 'settings', file))).toBe(true)
    }
  })

  it('does not invent account deletion, which the Core has no endpoint for', () => {
    const privacy = fs.readFileSync(path.join(ROOT, 'app', '(app)', 'settings', 'privacy.tsx'), 'utf8')
    expect(privacy).not.toMatch(/deleteAccount|\/users\/me['"]\s*,\s*\{\s*method:\s*'DELETE'/)
    expect(privacy).toContain('deletionNote')
  })
})

describe('routing smoke', () => {
  it('offers to create one when the account has none yet', async () => {
    useCapabilityStore.setState({ capabilities: baselineCapabilities(), loaded: true })
    scheduled.listScheduledTasks.mockResolvedValue({ tasks: [] })

    renderRouter({ scheduled: ScheduledScreen }, { initialUrl: '/scheduled' })

    expect(await screen.findByText(pt.scheduled.emptyTitle)).toBeTruthy()
    expect(screen.getByLabelText(pt.scheduled.new)).toBeTruthy()
  })

  it('shows what the Core says about a task, not what the device assumes', async () => {
    useCapabilityStore.setState({ capabilities: baselineCapabilities(), loaded: true })
    scheduled.listScheduledTasks.mockResolvedValue({
      tasks: [
        {
          id: 't1',
          title: 'Resumo da manhã',
          instruction: 'Resuma minhas prioridades.',
          recurrence: { kind: 'daily', time: '09:00', timezone: 'America/Cuiaba' },
          is_active: true,
          next_run_at: '2026-09-17T12:00:00Z',
          last_run_at: null,
          last_outcome: null,
          last_run: { job_id: 'j1', status: 'completed', completed_at: null, excerpt: 'três prioridades' },
          created_at: null,
        },
      ],
    })

    renderRouter({ scheduled: ScheduledScreen }, { initialUrl: '/scheduled' })

    expect(await screen.findByText('Resumo da manhã')).toBeTruthy()
    expect(screen.getByText(pt.scheduled.active)).toBeTruthy()
    expect(screen.getByText('três prioridades')).toBeTruthy()
    // The recurrence and its time zone are the Core's, shown as sent.
    expect(screen.getByText(/09:00/)).toBeTruthy()
  })
})
