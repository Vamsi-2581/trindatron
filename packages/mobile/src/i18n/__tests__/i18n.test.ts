import AsyncStorage from '@react-native-async-storage/async-storage'
import * as Localization from 'expo-localization'

import {
  detectSystemLocale,
  LOCALE_STORAGE_KEY,
  localeFromLanguageTag,
  resolveLocale,
  t,
  translate,
  useLocaleStore,
} from '../index'

const getLocales = Localization.getLocales as jest.Mock

function systemLanguage(tag: string) {
  getLocales.mockReturnValue([{ languageTag: tag, languageCode: tag.split('-')[0], regionCode: tag.split('-')[1] ?? null }])
}

beforeEach(async () => {
  await AsyncStorage.clear()
  systemLanguage('pt-BR')
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt', hydrated: false })
})

describe('language detection', () => {
  it('maps the operating system language to a supported locale', () => {
    expect(localeFromLanguageTag('pt-BR')).toBe('pt')
    expect(localeFromLanguageTag('pt-PT')).toBe('pt')
    expect(localeFromLanguageTag('en-US')).toBe('en')
    expect(localeFromLanguageTag('en-GB')).toBe('en')
    expect(localeFromLanguageTag('es-MX')).toBe('es')
    expect(localeFromLanguageTag('es_ES')).toBe('es')
    // Unsupported languages fall back to Portuguese, like the Web.
    expect(localeFromLanguageTag('fr-FR')).toBe('pt')
    expect(localeFromLanguageTag(null)).toBe('pt')
  })

  it('reads the first preferred language from the device', () => {
    systemLanguage('en-GB')
    expect(detectSystemLocale()).toBe('en')
  })
})

describe('precedence and persistence', () => {
  it('starts in the system language when nothing was chosen', async () => {
    systemLanguage('es-AR')
    await useLocaleStore.getState().hydrate()
    expect(useLocaleStore.getState()).toMatchObject({ preference: 'auto', locale: 'es', hydrated: true })
  })

  it('a manual choice wins over the system language and survives a restart', async () => {
    systemLanguage('en-US')
    await useLocaleStore.getState().setPreference('es')
    expect(await AsyncStorage.getItem(LOCALE_STORAGE_KEY)).toBe('es')

    useLocaleStore.setState({ preference: 'auto', locale: 'pt', hydrated: false })
    await useLocaleStore.getState().hydrate()
    expect(useLocaleStore.getState()).toMatchObject({ preference: 'es', locale: 'es' })
  })

  it('going back to Automatic follows the system again, including later system changes', async () => {
    systemLanguage('en-US')
    await useLocaleStore.getState().hydrate()
    await useLocaleStore.getState().setPreference('pt')
    expect(useLocaleStore.getState().locale).toBe('pt')

    await useLocaleStore.getState().setPreference('auto')
    expect(useLocaleStore.getState().locale).toBe('en')

    systemLanguage('es-ES')
    useLocaleStore.getState().refreshSystemLocale()
    expect(useLocaleStore.getState().locale).toBe('es')
  })

  it('a system language change does not override a manual choice', async () => {
    await useLocaleStore.getState().setPreference('en')
    systemLanguage('es-ES')
    useLocaleStore.getState().refreshSystemLocale()
    expect(useLocaleStore.getState().locale).toBe('en')
  })

  it('ignores a corrupted stored value', async () => {
    await AsyncStorage.setItem(LOCALE_STORAGE_KEY, 'klingon')
    systemLanguage('en-US')
    await useLocaleStore.getState().hydrate()
    expect(useLocaleStore.getState()).toMatchObject({ preference: 'auto', locale: 'en' })
  })

  it('resolveLocale applies the rule directly', () => {
    expect(resolveLocale('auto', 'en')).toBe('en')
    expect(resolveLocale('es', 'en')).toBe('es')
  })
})

describe('translation', () => {
  it('interpolates parameters and follows the active locale', async () => {
    expect(translate('pt', 'home.greeting', { name: 'Ana' })).toBe('Olá, Ana.')
    expect(translate('en', 'home.greeting', { name: 'Ana' })).toBe('Hi, Ana.')
    expect(translate('es', 'home.greeting', { name: 'Ana' })).toBe('Hola, Ana.')

    await useLocaleStore.getState().setPreference('en')
    expect(t('nav.files')).toBe('Files')
  })

  it('shares the Web activity keys', () => {
    expect(translate('en', 'activity.chat.processing')).toBe('Processing')
    expect(translate('es', 'activity.tool.web_search')).toBe('Buscando en la web')
  })

  it('returns the key itself for an unknown key', () => {
    expect(translate('en', 'does.not.exist')).toBe('does.not.exist')
  })
})
