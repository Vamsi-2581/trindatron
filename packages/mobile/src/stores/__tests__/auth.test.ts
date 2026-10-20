import * as authApi from '@/services/api/auth'
import { getClientConfig } from '@/services/api/client'
import { ApiError } from '@/services/api/errors'
import type { User } from '@/services/api/types'
import * as cache from '@/services/cache'
import * as tokenStorage from '@/services/session/tokenStorage'

import { SESSION_EXPIRED_NOTICE, useAuthStore } from '../auth'

jest.mock('@/services/api/auth')
jest.mock('@/services/cache')
jest.mock('@/services/session/tokenStorage')

const api = authApi as jest.Mocked<typeof authApi>
const storage = tokenStorage as jest.Mocked<typeof tokenStorage>
const cached = cache as jest.Mocked<typeof cache>

const user: User = {
  id: 'u1',
  email: 'ana@example.com',
  full_name: 'Ana Souza',
  plan: 'pro',
  is_active: true,
  timezone: 'UTC',
  created_at: '2026-09-13T10:00:00',
}

const unauthorized = () => new ApiError({ kind: 'unauthorized', status: 401, message: 'x' })
const offline = () => new ApiError({ kind: 'network', status: null, message: 'x' })
const issued = (access: string, refresh: string | null) => ({
  access_token: access, token_type: 'bearer', user_id: 'u1', email: user.email, refresh_token: refresh, expires_in: 1800,
})

function expiredJwt(): string {
  const encode = (value: object) => btoa(JSON.stringify(value)).replace(/=+$/, '')
  return `${encode({ alg: 'HS256' })}.${encode({ sub: 'u1', exp: 1_000 })}.signature`
}

beforeEach(() => {
  jest.resetAllMocks()
  storage.writeToken.mockResolvedValue(undefined)
  storage.deleteToken.mockResolvedValue(undefined)
  cached.writeCache.mockResolvedValue(undefined)
  cached.clearCache.mockResolvedValue(undefined)
  cached.readCache.mockResolvedValue(null)
  api.updateTimezone.mockResolvedValue({ timezone: 'America/Sao_Paulo' })
  api.logout.mockResolvedValue(undefined)
  useAuthStore.setState({ status: 'booting', token: null, refreshToken: null, user: null, notice: null, storageUnavailable: false })
})

describe('restoring a session', () => {
  it('opens signed out only when there is really no stored session', async () => {
    storage.readToken.mockResolvedValue({ status: 'absent' })
    await useAuthStore.getState().boot()
    expect(useAuthStore.getState().status).toBe('signedOut')
    expect(api.getMe).not.toHaveBeenCalled()
  })

  it('an unreadable secure store (phone just unlocked) is not a sign-out: it retries', async () => {
    storage.readToken.mockResolvedValueOnce({ status: 'unavailable' })
    await useAuthStore.getState().boot()
    expect(useAuthStore.getState()).toMatchObject({ status: 'booting', storageUnavailable: true })
    expect(storage.deleteToken).not.toHaveBeenCalled()

    storage.readToken.mockResolvedValueOnce({ status: 'found', token: 'tok', refreshToken: 'r1' })
    api.getMe.mockResolvedValue(user)
    await useAuthStore.getState().boot()
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedIn', token: 'tok', refreshToken: 'r1', storageUnavailable: false })
  })

  it('restores the session after closing, reopening or reloading the app', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: 'tok', refreshToken: 'r1' })
    api.getMe.mockResolvedValue(user)
    await useAuthStore.getState().boot()
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedIn', token: 'tok', user })
    expect(api.getMe).toHaveBeenCalledWith({ notifyUnauthorized: false })
  })

  it('an access token that expired while the app was closed is renewed, not a sign-out', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: expiredJwt(), refreshToken: 'r1' })
    api.getMe.mockRejectedValueOnce(unauthorized()).mockResolvedValue(user)
    api.refreshSession.mockResolvedValue(issued('fresh', 'r2'))
    await useAuthStore.getState().boot()
    expect(api.refreshSession).toHaveBeenCalledWith('r1')
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedIn', token: 'fresh', refreshToken: 'r2', user })
    // Rotation: the new refresh token replaces the old one in secure storage.
    expect(storage.writeToken).toHaveBeenCalledWith('fresh', 'r2')
  })

  it('stays signed in offline with the last known profile', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: 'tok', refreshToken: 'r1' })
    api.getMe.mockRejectedValue(offline())
    cached.readCache.mockResolvedValue(user)
    await useAuthStore.getState().boot()
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedIn', user })
    expect(storage.deleteToken).not.toHaveBeenCalled()
  })

  it('a network failure while renewing keeps the session', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: 'old', refreshToken: 'r1' })
    api.getMe.mockRejectedValue(unauthorized())
    api.refreshSession.mockRejectedValue(offline())
    cached.readCache.mockResolvedValue(user)
    await useAuthStore.getState().boot()
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedIn', refreshToken: 'r1' })
    expect(storage.deleteToken).not.toHaveBeenCalled()
  })

  it('signs out only when the Core refuses the refresh token', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: 'old', refreshToken: 'revoked' })
    api.getMe.mockRejectedValue(unauthorized())
    api.refreshSession.mockRejectedValue(unauthorized())
    await useAuthStore.getState().boot()
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedOut', notice: SESSION_EXPIRED_NOTICE })
    expect(storage.deleteToken).toHaveBeenCalled()
    // The Core already ended it; nothing to revoke.
    expect(api.logout).not.toHaveBeenCalled()
  })

  it('a session saved before refresh tokens existed signs out when the Core rejects it', async () => {
    storage.readToken.mockResolvedValue({ status: 'found', token: 'legacy', refreshToken: null })
    api.getMe.mockRejectedValue(unauthorized())
    await useAuthStore.getState().boot()
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedOut', notice: SESSION_EXPIRED_NOTICE })
  })
})

describe('a 401 during use', () => {
  beforeEach(() => {
    useAuthStore.setState({ status: 'signedIn', token: 'tok', refreshToken: 'r1', user })
  })

  it('renews once for every request that failed together', async () => {
    let finish: () => void = () => undefined
    api.refreshSession.mockImplementation(() => new Promise((resolve) => (finish = () => resolve(issued('fresh', 'r2')))))
    const attempts = [
      getClientConfig().refreshAccessToken(),
      getClientConfig().refreshAccessToken(),
      getClientConfig().refreshAccessToken(),
    ]
    finish()
    expect(await Promise.all(attempts)).toEqual([true, true, true])
    expect(api.refreshSession).toHaveBeenCalledTimes(1)
    expect(useAuthStore.getState()).toMatchObject({ token: 'fresh', refreshToken: 'r2' })
  })

  it('is verified before anyone is signed out: a transient failure keeps the session', async () => {
    api.getMe.mockRejectedValue(offline())
    getClientConfig().onUnauthorized()
    await useAuthStore.getState().verifySession()
    expect(useAuthStore.getState().status).toBe('signedIn')
  })

  it('keeps the session when /auth/me still accepts the token', async () => {
    api.getMe.mockResolvedValue(user)
    await useAuthStore.getState().verifySession()
    expect(useAuthStore.getState().status).toBe('signedIn')
    expect(storage.deleteToken).not.toHaveBeenCalled()
  })

  it('a rejected token is renewed before the session is given up', async () => {
    api.getMe.mockRejectedValue(unauthorized())
    api.refreshSession.mockResolvedValue(issued('fresh', 'r2'))
    await useAuthStore.getState().verifySession()
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedIn', token: 'fresh' })
  })

  it('signs out only when the Core confirms the session is gone', async () => {
    api.getMe.mockRejectedValue(unauthorized())
    api.refreshSession.mockRejectedValue(unauthorized())
    await useAuthStore.getState().verifySession()
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedOut', notice: SESSION_EXPIRED_NOTICE })
  })

  it('does not sign out a newer session when a stale check fails', async () => {
    let fail: () => void = () => undefined
    api.getMe.mockImplementation(() => new Promise((_resolve, reject) => (fail = () => reject(unauthorized()))))
    const check = useAuthStore.getState().verifySession()
    useAuthStore.setState({ token: 'new-token', refreshToken: 'r9' })
    fail()
    await check
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedIn', token: 'new-token' })
  })
})

describe('signing in and out', () => {
  it('persists the access and refresh tokens only after the Core returns the profile', async () => {
    api.login.mockResolvedValue(issued('tok', 'r1'))
    api.getMe.mockResolvedValue(user)
    await useAuthStore.getState().signIn(user.email, 'secret-pass')
    expect(storage.writeToken).toHaveBeenCalledWith('tok', 'r1')
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedIn', token: 'tok', refreshToken: 'r1' })
  })

  it('keeps no token when the profile cannot be loaded after login', async () => {
    api.login.mockResolvedValue(issued('tok', 'r1'))
    api.getMe.mockRejectedValue(new ApiError({ kind: 'server', status: 500, message: 'x' }))
    await expect(useAuthStore.getState().signIn(user.email, 'secret-pass')).rejects.toBeInstanceOf(ApiError)
    expect(storage.writeToken).not.toHaveBeenCalled()
    expect(useAuthStore.getState()).toMatchObject({ token: null, refreshToken: null })
  })

  it('an explicit sign-out revokes the login on the server and wipes this device', async () => {
    useAuthStore.setState({ status: 'signedIn', token: 'tok', refreshToken: 'r1', user })
    await useAuthStore.getState().signOut()
    expect(api.logout).toHaveBeenCalledWith('r1')
    expect(useAuthStore.getState()).toMatchObject({ status: 'signedOut', token: null, refreshToken: null, user: null, notice: null })
    expect(storage.deleteToken).toHaveBeenCalled()
    expect(cached.clearCache).toHaveBeenCalled()
  })

  it('signing out offline still ends the session on this device', async () => {
    useAuthStore.setState({ status: 'signedIn', token: 'tok', refreshToken: 'r1', user })
    api.logout.mockRejectedValue(offline())
    await useAuthStore.getState().signOut()
    expect(useAuthStore.getState().status).toBe('signedOut')
    expect(storage.deleteToken).toHaveBeenCalled()
  })
})
