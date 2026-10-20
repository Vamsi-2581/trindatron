import { fireEvent, screen, waitFor } from '@testing-library/react-native'
import { Stack } from 'expo-router'
import { renderRouter } from 'expo-router/testing-library'
import { Text } from 'react-native'

import { baselineCapabilities } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import { useStackScreenOptions } from '@/features/navigation/stackHeader'
import { useLocaleStore } from '@/i18n'
import * as projectsApi from '@/services/api/projects'
import type { Project } from '@/services/api/types'
import * as usageApi from '@/services/api/usage'
import { useAuthStore } from '@/stores/auth'

import ProfileScreen from '../../app/(app)/(tabs)/profile'
import ProjectsScreen from '../../app/(app)/(tabs)/projects'
import UsageScreen from '../../app/(app)/usage'

jest.mock('@/services/api/usage')
jest.mock('@/services/api/projects')

/** The app stack with its own header, so actions a screen puts in the header can be pressed. */
function AppStack() {
  return <Stack screenOptions={useStackScreenOptions()} />
}

const usage = usageApi as jest.Mocked<typeof usageApi>
const projects = projectsApi as jest.Mocked<typeof projectsApi>

const project: Project = {
  id: 'p1',
  user_id: 'u1',
  name: 'Lançamento',
  description: 'Campanha de setembro',
  instructions: null,
  is_archived: false,
  created_at: '2026-09-13T10:00:00',
  updated_at: '2026-09-13T10:00:00',
  files: [],
}

function mockCapacity() {
  usage.getCapacity.mockResolvedValue({
    tier: 'normal',
    capacity_remaining_pct: 62,
    session: { active: true, pct_used: 31, resets_at: new Date(Date.now() + 2 * 3600_000).toISOString() },
    weekly: { pct_used: 91, resets_at: new Date(Date.now() + 3 * 86_400_000).toISOString() },
    active_agents: 1,
    optimizing: false,
  })
  usage.getEntitlement.mockResolvedValue({
    plan: 'pro',
    quota: { daily_tcu_limit: 100, weekly_tcu_limit: 500, monthly_tcu_limit: null, max_tcu_per_request: 10 },
    features: { supports_agent_mode: true, supports_trinity_code: true, supports_trinity_work: false, supports_image_generation: false },
    limits: { max_projects: 10, max_agents: 3 },
  })
}

beforeEach(() => {
  jest.resetAllMocks()
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
  useCapabilityStore.setState({ capabilities: baselineCapabilities(), loaded: true, userId: 'u1', load: jest.fn(async () => undefined) })
  useAuthStore.setState({
    status: 'signedIn',
    token: 'tok',
    notice: null,
    user: { id: 'u1', email: 'ana@example.com', full_name: 'Ana', plan: 'pro', is_active: true, timezone: null, created_at: '2026-09-13T10:00:00' },
  })
})

describe('Usage screen', () => {
  it('shows the session and the week with percentage, remaining and reset time from the Core — no TCU', async () => {
    mockCapacity()
    renderRouter({ usage: UsageScreen, billing: () => <Text>billing</Text> }, { initialUrl: '/usage' })

    expect(await screen.findByText('Sessão atual')).toBeTruthy()
    expect(screen.getByText('31% usado')).toBeTruthy()
    expect(screen.getByText('69% restante')).toBeTruthy()
    expect(screen.getByText(/^Redefine (às \d{2}:\d{2}|.+, \d{2}:\d{2})$/)).toBeTruthy()

    expect(screen.getByText('Esta semana')).toBeTruthy()
    expect(screen.getByText('91% usado')).toBeTruthy()
    expect(screen.getByText(/^Reinicia .+, \d{2}:\d{2}$/)).toBeTruthy()

    expect(screen.getByText('Pro')).toBeTruthy()
    expect(await screen.findByText('Modo agente')).toBeTruthy()
    expect(screen.queryByText(/TCU|token|US\$/i)).toBeNull()
  })

  it('formats the same data in English', async () => {
    useLocaleStore.setState({ locale: 'en' })
    mockCapacity()
    renderRouter({ usage: UsageScreen }, { initialUrl: '/usage' })

    expect(await screen.findByText('Current session')).toBeTruthy()
    expect(screen.getByText('31% used')).toBeTruthy()
    // Session and week both read "Resets …" in English, each with a 12-hour clock.
    expect(screen.getAllByText(/^Resets (at )?(.+, )?\d{2}:\d{2} (AM|PM)$/)).toHaveLength(2)
    expect(screen.getByText('This week')).toBeTruthy()
  })

  it('shows images and videos available today only as the Core reports them', async () => {
    mockCapacity()
    renderRouter({ usage: UsageScreen }, { initialUrl: '/usage' })
    expect(await screen.findByText('Sessão atual')).toBeTruthy()
    expect(screen.queryByText('Imagens disponíveis hoje')).toBeNull()

    const capabilities = baselineCapabilities()
    capabilities.image_generation = { available: true, dailyQuota: { limit: 10, remaining: 7, resetsAt: null }, limits: null }
    capabilities.video_generation = { available: true, dailyQuota: { limit: 2, remaining: 2, resetsAt: null }, limits: null }
    useCapabilityStore.setState({ capabilities })

    expect(await screen.findByText('Imagens disponíveis hoje')).toBeTruthy()
    expect(screen.getByText('7 de 10')).toBeTruthy()
    expect(screen.getByText('Vídeos disponíveis hoje')).toBeTruthy()
    expect(screen.getByText('2 de 2')).toBeTruthy()
    expect(screen.queryByText(/TCU|token/i)).toBeNull()
  })

  it('lists under "Incluído no seu plano" only what the plan includes, and the rest in its own section', async () => {
    mockCapacity() // agent mode and development included; autonomous execution and images not
    renderRouter({ usage: UsageScreen }, { initialUrl: '/usage' })

    expect(await screen.findByText('Incluído no seu plano')).toBeTruthy()
    expect(screen.getByText('Não incluído no seu plano')).toBeTruthy()
    expect(screen.getByLabelText('Modo agente: incluído')).toBeTruthy()
    expect(screen.getByLabelText('Desenvolvimento: incluído')).toBeTruthy()
    expect(screen.getByLabelText('Execução autônoma: não incluído')).toBeTruthy()
    expect(screen.getByLabelText('Geração de imagens: não incluído')).toBeTruthy()
    // On the device, the "included" list showed the missing ones with a minus sign.
    expect(screen.queryByLabelText('Execução autônoma: incluído')).toBeNull()
  })

  it('has no "not included" section when the plan includes everything', async () => {
    mockCapacity()
    usage.getEntitlement.mockResolvedValue({
      plan: 'max',
      quota: { daily_tcu_limit: 1000, weekly_tcu_limit: 5000, monthly_tcu_limit: null, max_tcu_per_request: 100 },
      features: { supports_agent_mode: true, supports_trinity_code: true, supports_trinity_work: true, supports_image_generation: true },
      limits: { max_projects: null, max_agents: 10 },
    })
    renderRouter({ usage: UsageScreen }, { initialUrl: '/usage' })
    expect(await screen.findByText('Incluído no seu plano')).toBeTruthy()
    expect(screen.queryByText('Não incluído no seu plano')).toBeNull()
  })

  it('offers a retry when capacity cannot be loaded', async () => {
    usage.getCapacity.mockRejectedValue(new Error('offline'))
    usage.getEntitlement.mockRejectedValue(new Error('offline'))
    renderRouter({ usage: UsageScreen }, { initialUrl: '/usage' })
    expect(await screen.findByText('Tentar novamente')).toBeTruthy()
  })
})

describe('Profile language', () => {
  it('is one compact row that opens a short list, and the choice persists', async () => {
    renderRouter({ profile: ProfileScreen }, { initialUrl: '/profile' })

    expect(await screen.findByText('Idioma')).toBeTruthy()
    expect(screen.getByText('Automático · Português')).toBeTruthy()
    // The options are not laid out on the page until the row is tapped.
    expect(screen.queryByText('English')).toBeNull()

    fireEvent.press(screen.getByText('Idioma'))
    expect(await screen.findByLabelText('Automático, Português')).toBeTruthy()
    expect(screen.getByLabelText('Português')).toBeTruthy()
    expect(screen.getByLabelText('Español')).toBeTruthy()

    fireEvent.press(screen.getByLabelText('English'))
    // Persistence of the choice itself is covered in src/i18n/__tests__/i18n.test.ts.
    await waitFor(() => expect(useLocaleStore.getState()).toMatchObject({ preference: 'en', locale: 'en' }))
    expect(await screen.findByText('Language')).toBeTruthy()
    expect(screen.getByText('English')).toBeTruthy()
  })
})

describe('Projects screen', () => {
  it('lists projects from the Core and creates a new one', async () => {
    projects.listProjects.mockResolvedValue({ projects: [project], total: 1 })
    projects.createProject.mockResolvedValue({ ...project, id: 'p2', name: 'Pesquisa' })

    renderRouter(
      { _layout: AppStack, projects: ProjectsScreen, 'project/[projectId]': () => <Text>project-detail</Text> },
      { initialUrl: '/projects' },
    )

    expect(await screen.findByText('Lançamento')).toBeTruthy()

    // With projects listed, creating one lives in the header. The native header config keeps a hidden copy of the
    // header actions (the Jest stand-in renders it too); ours comes first.
    fireEvent.press(screen.getAllByRole('button', { name: 'Novo projeto' })[0]!)
    fireEvent.changeText(screen.getByLabelText('Nome'), 'Pesquisa')
    fireEvent.press(screen.getByText('Criar projeto'))

    await waitFor(() => expect(projects.createProject).toHaveBeenCalledWith({ name: 'Pesquisa', description: null }))
    expect(await screen.findByText('project-detail')).toBeTruthy()
    expect(screen).toHavePathname('/project/p2')
  })
})
