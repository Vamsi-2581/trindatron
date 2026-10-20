import { create } from 'zustand'

import * as authApi from '@/services/api/auth'
import { configureApiClient } from '@/services/api/client'
import { ApiError } from '@/services/api/errors'
import type { AuthTokenResponse, User } from '@/services/api/types'
import { markBoot, timeBootStep } from '@/services/bootTimeline'
import { clearCache, readCache, writeCache } from '@/services/cache'
import { deleteToken, readToken, writeToken } from '@/services/session/tokenStorage'

export type AuthStatus = 'booting' | 'signedOut' | 'signedIn'

/** i18n key shown on the sign-in screen after the Core ends a session. */
export const SESSION_EXPIRED_NOTICE = 'auth.sessionExpired'

/**
 * Session policy: once signed in, the user stays signed in until they tap
 * "Sair" or the Core proves the session is gone. Backgrounding, locking the
 * phone, reloading the bundle, restarting the app or a short-lived access
 * token expiring never sign anyone out.
 *
 * The Core issues a 30-minute access token and a rotating refresh token
 * (POST /auth/refresh). An expired access token is renewed transparently —
 * one shared renewal however many requests failed together. "Irrecoverable"
 * means exactly one thing: the Core refused the refresh token (revoked,
 * replayed, or unused for its whole inactivity window).
 */
interface AuthState {
  status: AuthStatus
  token: string | null
  refreshToken: string | null
  user: User | null
  /** i18n key of a message for the sign-in screen, or null. */
  notice: string | null
  /** The secure store could not be read (e.g. device just unlocked); boot must be retried. */
  storageUnavailable: boolean
  boot(): Promise<void>
  signIn(email: string, password: string): Promise<void>
  signUp(email: string, password: string, fullName: string): Promise<void>
  signInWithToken(token: string, refreshToken?: string | null): Promise<void>
  signOut(notice?: string | null): Promise<void>
  /** Renew the access token. True when a new token is in place. */
  refreshAccess(): Promise<boolean>
  verifySession(): Promise<void>
  refreshUser(): Promise<User | null>
  clearNotice(): void
}

function detectTimezone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null
  } catch {
    return null
  }
}

/** Same rule as the Web: the Core owns the weekly reset, the client only reports its zone. */
function syncTimezone(user: User): void {
  const timezone = detectTimezone()
  if (timezone && timezone !== user.timezone) {
    authApi.updateTimezone(timezone).catch(() => undefined)
  }
}

const isUnauthorized = (error: unknown) => error instanceof ApiError && error.kind === 'unauthorized'

let verification: Promise<void> | null = null
let renewal: Promise<boolean> | null = null

export const useAuthStore = create<AuthState>()((set, get) => {
  async function establish(token: string, refreshToken: string | null): Promise<void> {
    set({ token, refreshToken })
    try {
      const user = await authApi.getMe({ notifyUnauthorized: false })
      await writeToken(token, refreshToken)
      await writeCache('user', user)
      set({ status: 'signedIn', user, notice: null, storageUnavailable: false })
      syncTimezone(user)
    } catch (error) {
      set({ token: null, refreshToken: null })
      throw error
    }
  }

  const fromResponse = (response: AuthTokenResponse) => establish(response.access_token, response.refresh_token ?? null)

  function restored(user: User | null) {
    set({ status: 'signedIn', user })
    markBoot('restoreDone')
    if (user) markBoot('profile')
  }

  return {
    status: 'booting',
    token: null,
    refreshToken: null,
    user: null,
    notice: null,
    storageUnavailable: false,

    async boot() {
      markBoot('restoreStart')
      const stored = await timeBootStep('secureStoreRead', readToken())
      if (stored.status === 'unavailable') {
        set({ status: 'booting', storageUnavailable: true })
        return
      }
      if (stored.status === 'absent') {
        set({ status: 'signedOut', storageUnavailable: false })
        markBoot('restoreDone')
        return
      }

      set({ token: stored.token, refreshToken: stored.refreshToken, storageUnavailable: false })
      try {
        // The token is checked by the Core, never by the device clock.
        const user = await timeBootStep('profileRequest', authApi.getMe({ notifyUnauthorized: false }))
        await writeCache('user', user)
        restored(user)
        syncTimezone(user)
      } catch (error) {
        if (isUnauthorized(error)) {
          // Usually just an access token that expired while the app was closed.
          if (await timeBootStep('tokenRefresh', get().refreshAccess())) {
            try {
              const user = await authApi.getMe({ notifyUnauthorized: false })
              await writeCache('user', user)
              restored(user)
              syncTimezone(user)
              return
            } catch (retryError) {
              if (isUnauthorized(retryError)) {
                await get().signOut(SESSION_EXPIRED_NOTICE)
                return
              }
            }
          } else if (get().status === 'signedOut') {
            return
          } else if (!get().refreshToken) {
            await get().signOut(SESSION_EXPIRED_NOTICE)
            return
          }
        }
        // Offline, timeout or Core unavailable: stay signed in with the last known profile.
        restored(get().user ?? (await readCache<User>('user')))
      }
    },

    async signIn(email, password) {
      await fromResponse(await authApi.login(email, password))
    },

    async signUp(email, password, fullName) {
      await fromResponse(await authApi.register(email, password, fullName))
    },

    signInWithToken(token, refreshToken = null) {
      return establish(token, refreshToken)
    },

    async signOut(notice = null) {
      const refreshToken = get().refreshToken
      set({ status: 'signedOut', token: null, refreshToken: null, user: null, notice, storageUnavailable: false })
      // Revoke on the server when this is the person's choice; a Core-ended session is already gone there.
      if (refreshToken && notice === null) await authApi.logout(refreshToken).catch(() => undefined)
      await Promise.all([deleteToken(), clearCache()])
    },

    refreshAccess() {
      const refreshToken = get().refreshToken
      if (!refreshToken) return Promise.resolve(false)
      if (renewal) return renewal
      renewal = (async () => {
        try {
          const renewed = await authApi.refreshSession(refreshToken)
          // A sign-out or another sign-in happened meanwhile: this result is stale.
          if (get().refreshToken !== refreshToken) return false
          const nextRefresh = renewed.refresh_token ?? refreshToken
          set({ token: renewed.access_token, refreshToken: nextRefresh })
          await writeToken(renewed.access_token, nextRefresh).catch(() => undefined)
          return true
        } catch (error) {
          if (isUnauthorized(error) && get().refreshToken === refreshToken) {
            await get().signOut(SESSION_EXPIRED_NOTICE)
          }
          return false
        } finally {
          renewal = null
        }
      })()
      return renewal
    },

    /**
     * Called when a request still gets a 401 after renewal was attempted (or
     * could not be). One 401 is not proof the session is over, so the session
     * is confirmed against /auth/me before anyone is signed out.
     */
    verifySession() {
      const token = get().token
      if (!token || get().status !== 'signedIn') return Promise.resolve()
      if (verification) return verification
      verification = (async () => {
        try {
          await authApi.getMe({ notifyUnauthorized: false })
        } catch (error) {
          if (isUnauthorized(error) && get().token === token) {
            if (get().refreshToken && (await get().refreshAccess())) return
            if (get().token === token && get().status === 'signedIn') await get().signOut(SESSION_EXPIRED_NOTICE)
          }
        } finally {
          verification = null
        }
      })()
      return verification
    },

    async refreshUser() {
      try {
        const user = await authApi.getMe()
        await writeCache('user', user)
        set({ user })
        return user
      } catch {
        return get().user
      }
    },

    clearNotice() {
      set({ notice: null })
    },
  }
})

configureApiClient({
  getToken: () => useAuthStore.getState().token,
  onUnauthorized: () => {
    void useAuthStore.getState().verifySession()
  },
  refreshAccessToken: () => useAuthStore.getState().refreshAccess(),
})
