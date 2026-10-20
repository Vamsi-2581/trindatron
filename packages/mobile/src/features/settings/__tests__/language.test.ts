import { useLocaleStore } from '@/i18n'
import { getPreferences, updatePreferences, type Preferences } from '@/services/api/settings'

import { chooseLanguage, syncLanguageWithAccount } from '../language'

jest.mock('@/services/api/settings', () => ({ getPreferences: jest.fn(), updatePreferences: jest.fn() }))
const mockGet = getPreferences as jest.MockedFunction<typeof getPreferences>
const mockUpdate = updatePreferences as jest.MockedFunction<typeof updatePreferences>

const account = (language: Preferences['language']): Preferences => ({
  language,
  response_style: 'balanced',
  send_on_enter: true,
  voice_auto_play: false,
  memory_enabled: true,
  disabled_plugins: [],
})

beforeEach(() => {
  mockGet.mockReset()
  mockUpdate.mockReset().mockImplementation(async (changes) => account(changes.language ?? 'auto'))
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
})

describe('chooseLanguage', () => {
  it('changes the app at once and records the same choice on the account', async () => {
    await chooseLanguage('en')
    expect(useLocaleStore.getState()).toMatchObject({ preference: 'en', locale: 'en' })
    expect(mockUpdate).toHaveBeenCalledWith({ language: 'en' })
  })

  it('keeps the choice on the phone when the account cannot be reached', async () => {
    mockUpdate.mockRejectedValueOnce(new Error('offline'))
    await chooseLanguage('es')
    expect(useLocaleStore.getState().locale).toBe('es')
  })

  it('"automatic" goes back to the phone language', async () => {
    useLocaleStore.setState({ preference: 'en', systemLocale: 'pt', locale: 'en' })
    await chooseLanguage('auto')
    expect(useLocaleStore.getState().locale).toBe('pt')
  })
})

describe('syncLanguageWithAccount', () => {
  it('adopts a language chosen on the account (here or on the Web)', async () => {
    mockGet.mockResolvedValueOnce(account('es'))
    await syncLanguageWithAccount()
    expect(useLocaleStore.getState()).toMatchObject({ preference: 'es', locale: 'es' })
    expect(mockUpdate).not.toHaveBeenCalled()
  })

  it('records the phone choice when the account is still automatic, instead of discarding it', async () => {
    useLocaleStore.setState({ preference: 'en', systemLocale: 'pt', locale: 'en' })
    mockGet.mockResolvedValueOnce(account('auto'))
    await syncLanguageWithAccount()
    expect(mockUpdate).toHaveBeenCalledWith({ language: 'en' })
    expect(useLocaleStore.getState().locale).toBe('en')
  })

  it('does nothing when both already agree', async () => {
    mockGet.mockResolvedValueOnce(account('auto'))
    await syncLanguageWithAccount()
    expect(mockUpdate).not.toHaveBeenCalled()
    expect(useLocaleStore.getState().locale).toBe('pt')
  })
})
