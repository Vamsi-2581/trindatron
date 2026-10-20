import { screen } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'

import { useLocaleStore } from '@/i18n'
import * as agentsApi from '@/services/api/agents'
import type { AgentDefinition } from '@/services/api/types'

import AgentsScreen from '../../app/(app)/(tabs)/agents'

jest.mock('@/services/api/agents')
const agents = agentsApi as jest.Mocked<typeof agentsApi>

const writer = (overrides: Partial<AgentDefinition> = {}): AgentDefinition => ({
  id: 'a1',
  user_id: 'u1',
  name: 'Redator',
  description: 'Escreve posts',
  avatar_emoji: null,
  system_instructions: null,
  skill_ids: [],
  model_preference: 'balanced',
  is_archived: false,
  created_at: '2026-09-13T10:00:00',
  updated_at: '2026-09-13T10:00:00',
  ...overrides,
})

beforeEach(() => {
  jest.resetAllMocks()
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
  agents.listAgentInstances.mockResolvedValue({ instances: [], total: 0 })
  agents.listExecutionJobs.mockResolvedValue({ jobs: [], total: 0 })
})

describe('Agentes', () => {
  it('with nothing yet, shows one empty state for the page, not one per section', async () => {
    agents.listAgentDefinitions.mockResolvedValue({ agents: [], total: 0 })
    renderRouter({ agents: AgentsScreen }, { initialUrl: '/agents' })

    expect(await screen.findByText('Nenhum agente ainda')).toBeTruthy()
    expect(screen.queryByText('Agentes persistentes')).toBeNull()
    expect(screen.queryByText('Execuções recentes')).toBeNull()
  })

  it('an archived agent is not an agent here', async () => {
    agents.listAgentDefinitions.mockResolvedValue({ agents: [writer({ is_archived: true })], total: 1 })
    renderRouter({ agents: AgentsScreen }, { initialUrl: '/agents' })
    expect(await screen.findByText('Nenhum agente ainda')).toBeTruthy()
  })

  it('with an agent, lists it and marks the empty sections with a quiet line', async () => {
    agents.listAgentDefinitions.mockResolvedValue({ agents: [writer()], total: 1 })
    renderRouter({ agents: AgentsScreen }, { initialUrl: '/agents' })

    expect(await screen.findByText('Redator')).toBeTruthy()
    expect(screen.getByText('Nenhum agente persistente em execução.')).toBeTruthy()
    expect(screen.getByText('Nenhuma execução ainda.')).toBeTruthy()
    expect(screen.queryByText('Nenhum agente ainda')).toBeNull()
  })
})
