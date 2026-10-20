import { fireEvent, screen, waitFor } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'

import { useLocaleStore } from '@/i18n'
import * as settingsApi from '@/services/api/settings'
import type { Preferences } from '@/services/api/settings'

import SettingsScreen from '../../app/(app)/settings'
import MemoriesScreen from '../../app/(app)/settings/memories'

jest.mock('@/services/api/settings')
const settings = settingsApi as jest.Mocked<typeof settingsApi>

const preferences = (memory_enabled: boolean): Preferences => ({
  language: 'auto',
  response_style: 'balanced',
  send_on_enter: true,
  voice_auto_play: false,
  memory_enabled,
  disabled_plugins: [],
})

beforeEach(() => {
  jest.resetAllMocks()
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
  settings.listMemories.mockResolvedValue([])
})

describe('Memórias switch', () => {
  it('describes the state it is in — a switch that is on never reads "Desligada"', async () => {
    settings.getPreferences.mockResolvedValue(preferences(true))
    settings.updatePreferences.mockResolvedValue(preferences(false))
    renderRouter({ memories: MemoriesScreen }, { initialUrl: '/memories' })

    expect(await screen.findByText(/^Ligada: /)).toBeTruthy()
    expect(screen.queryByText(/^Desligada/)).toBeNull()

    fireEvent(screen.getByRole('switch'), 'valueChange', false)
    await waitFor(() => expect(screen.getByText(/^Desligada: /)).toBeTruthy())
    expect(screen.queryByText(/^Ligada: /)).toBeNull()
  })
})

describe('Configurações', () => {
  it('has no switch that promises reading every answer aloud — listening is always a tap on "Ouvir"', async () => {
    settings.getPreferences.mockResolvedValue(preferences(true))
    renderRouter({ settings: SettingsScreen }, { initialUrl: '/settings' })
    expect(await screen.findByText('Estilo')).toBeTruthy()
    expect(screen.queryByText(/Ler respostas em voz alta/)).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
  })
})
