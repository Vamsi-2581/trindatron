import { resolveApiBaseUrl } from '@/config/env'
import { recordingPart } from '@/features/voice/recordingPart'
import { useLocaleStore } from '@/i18n'

import { formatClock, formatRelativeTime, formatTimeUntil, isSameLocalDay, parseServerDate } from '../dates'
import { conversationTitle, formatBytes, formatPercent, planLabel } from '../format'

beforeEach(() => {
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
})

describe('dates', () => {
  it('reads the Core naive timestamps as UTC', () => {
    expect(parseServerDate('2026-09-13T18:41:58.140089').toISOString()).toBe('2026-09-13T18:41:58.140Z')
    expect(parseServerDate('2026-09-13T18:41:58+00:00').toISOString()).toBe('2026-09-13T18:41:58.000Z')
  })

  it('formats relative and upcoming times in the active language', () => {
    const now = Date.parse('2026-09-13T12:00:00Z')
    expect(formatRelativeTime('2026-09-13T11:59:30', now)).toBe('agora')
    expect(formatRelativeTime('2026-09-13T11:15:00', now)).toBe('há 45 min')
    expect(formatRelativeTime('2026-09-12T10:00:00', now)).toBe('ontem')
    expect(formatTimeUntil('2026-09-13T15:00:00Z', now)).toBe('em 3 h')
    expect(formatTimeUntil('2026-09-15T12:00:00Z', now)).toBe('em 2 dias')

    useLocaleStore.setState({ locale: 'en' })
    expect(formatRelativeTime('2026-09-13T11:15:00', now)).toBe('45 min ago')
    expect(formatTimeUntil('2026-09-15T12:00:00Z', now)).toBe('in 2 days')
  })

  it('formats clock times with the locale conventions', () => {
    expect(formatClock('2026-09-13T15:05:00Z')).toMatch(/^\d{2}:\d{2}$/)
    useLocaleStore.setState({ locale: 'en' })
    expect(formatClock('2026-09-13T15:05:00Z')).toMatch(/(AM|PM)$/)
  })

  it('knows whether an instant falls on the local today', () => {
    const now = Date.now()
    expect(isSameLocalDay(new Date(now).toISOString(), now)).toBe(true)
    expect(isSameLocalDay(new Date(now + 3 * 86_400_000).toISOString(), now)).toBe(false)
  })
})

describe('format', () => {
  it('formats sizes, percentages and plan names', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1536)).toBe('1,5 KB')
    expect(formatBytes(10 * 1024 * 1024)).toBe('10 MB')
    expect(formatPercent(61.6)).toBe('62%')
    expect(formatPercent(null)).toBe('—')
    expect(planLabel('pro')).toBe('Pro')
  })

  it('names conversations the Core has not titled yet, in the user language', () => {
    expect(conversationTitle('New conversation')).toBe('Nova conversa')
    expect(conversationTitle(null)).toBe('Nova conversa')
    expect(conversationTitle('Plano de lançamento')).toBe('Plano de lançamento')
    useLocaleStore.setState({ locale: 'es' })
    expect(conversationTitle('New conversation')).toBe('Nueva conversación')
  })
})

describe('config helpers', () => {
  it('never guesses a production API host', () => {
    expect(resolveApiBaseUrl(' https://api.example.com/ ', false, 'ios')).toBe('https://api.example.com')
    expect(resolveApiBaseUrl(undefined, false, 'ios')).toBeNull()
    expect(resolveApiBaseUrl(undefined, true, 'android')).toBe('http://10.0.2.2:8000')
    expect(resolveApiBaseUrl(undefined, true, 'web')).toBe('http://localhost:8000')
  })

  it('labels voice recordings with a type the Core accepts', () => {
    expect(recordingPart('file:///a.m4a', 'ios')).toMatchObject({ mimeType: 'audio/mp4', name: 'voice.m4a' })
    expect(recordingPart('blob:x', 'web')).toMatchObject({ mimeType: 'audio/webm' })
  })
})
