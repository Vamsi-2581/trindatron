import { act, screen, waitFor } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'
import { AppState, Text, type AppStateStatus } from 'react-native'

import * as authApi from '@/services/api/auth'
import type { User } from '@/services/api/types'
import * as tokenStorage from '@/services/session/tokenStorage'
import { SESSION_EXPIRED_NOTICE, useAuthStore } from '@/stores/auth'

import RootLayout from '../../app/_layout'
import AuthLayout from '../../app/(auth)/_layout'
import LoginScreen from '../../app/(auth)/login'
import AppLayout from '../../app/(app)/_layout'
import TabsLayout from '../../app/(app)/(tabs)/_layout'

jest.mock('@/services/session/tokenStorage')
jest.mock('@/services/api/auth')
jest.mock('expo-web-browser', () => ({ maybeCompleteAuthSession: jest.fn() }))

const storage = tokenStorage as jest.Mocked<typeof tokenStorage>
const api = authApi as jest.Mocked<typeof authApi>

const user: User = {
  id: 'u1',
  email: 'ana@example.com',
  full_name: 'Ana',
  plan: 'pro',
  is_active: true,
  timezone: 'America/Sao_Paulo',
  created_at: '2026-09-13T10:00:00',
}

const routes = {
  _layout: RootLayout,
  '(auth)/_layout': AuthLayout,
  '(auth)/login': LoginScreen,
  '(auth)/register': () => <Text>register</Text>,
  '(app)/_layout': AppLayout,
  '(app)/(tabs)/_layout': TabsLayout,
  '(app)/(tabs)/index': () => <Text>home-screen</Text>,
  '(app)/(tabs)/agents': () => <Text>agents</Text>,
  '(app)/(tabs)/projects': () => <Text>projects</Text>,
  '(app)/(tabs)/files': () => <Text>files</Text>,
  '(app)/(tabs)/profile': () => <Text>profile</Text>,
  '(app)/usage': () => <Text>usage-screen</Text>,
}

let appStateListener: ((state: AppStateStatus) => void) | null = null

beforeEach(() => {
  jest.resetAllMocks()
  appStateListener = null
  jest.spyOn(AppState, 'addEventListener').mockImplementation((_type, listener) => {
    appStateListener = listener as (state: AppStateStatus) => void
    return { remove: jest.fn() } as never
  })
  storage.deleteToken.mockResolvedValue(undefined)
  storage.writeToken.mockResolvedValue(undefined)
  api.updateTimezone.mockResolvedValue({ timezone: user.timezone })
  useAuthStore.setState({ status: 'booting', token: null, user: null, notice: null, storageUnavailable: false })
})

async function emitAppState(state: AppStateStatus) {
  await act(async () => {
    appStateListener?.(state)
  })
}

describe('protected navigation', () => {
  it('sends a signed-out visitor to login, even from a deep link into the app', async () => {
    storage.readToken.mockResolvedValue({ status: 'absent' })
    renderRouter(routes, { initialUrl: '/usage' })

    await waitFor(() => expect(screen.getByLabelText('E-mail')).toBeTruthy())
    expect(screen.queryByText('usage-screen')).toBeNull()
    expect(screen).toHavePathname('/login')
    // No Google button while the Core has no Google session contract.
    expect(screen.queryByText('Continuar com Google')).toBeNull()
  })

  it('opens the app for a restored session and keeps auth screens out of reach', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: 'tok', refreshToken: 'r1' })
    api.getMe.mockResolvedValue(user)
    renderRouter(routes, { initialUrl: '/login' })

    await waitFor(() => expect(screen.getByText('home-screen')).toBeTruthy())
    expect(screen.queryByLabelText('E-mail')).toBeNull()
  })

  it('backgrounding, locking and returning to the app keep the user signed in', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: 'tok', refreshToken: 'r1' })
    api.getMe.mockResolvedValue(user)
    renderRouter(routes, { initialUrl: '/' })
    await waitFor(() => expect(screen.getByText('home-screen')).toBeTruthy())

    await emitAppState('inactive')
    await emitAppState('background')
    await emitAppState('active')

    expect(screen.getByText('home-screen')).toBeTruthy()
    expect(useAuthStore.getState().status).toBe('signedIn')
    expect(storage.deleteToken).not.toHaveBeenCalled()
  })

  it('retries a secure-store read that failed while the phone was locked when the app becomes active', async () => {
    storage.readToken.mockResolvedValueOnce({ status: 'unavailable' }).mockResolvedValue({ status: 'found', token: 'tok', refreshToken: 'r1' })
    api.getMe.mockResolvedValue(user)
    renderRouter(routes, { initialUrl: '/' })

    await waitFor(() => expect(useAuthStore.getState().storageUnavailable).toBe(true))
    expect(screen.queryByLabelText('E-mail')).toBeNull()

    await emitAppState('active')
    await waitFor(() => expect(screen.getByText('home-screen')).toBeTruthy())
  })

  it('returns to login with a translated notice when the Core ends the session', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: 'tok', refreshToken: 'r1' })
    api.getMe.mockResolvedValue(user)
    renderRouter(routes, { initialUrl: '/' })
    await waitFor(() => expect(screen.getByText('home-screen')).toBeTruthy())

    await act(() => useAuthStore.getState().signOut(SESSION_EXPIRED_NOTICE))

    await waitFor(() => expect(screen.getByText('Sua sessão terminou. Entre novamente.')).toBeTruthy())
  })
})
