import * as SecureStore from 'expo-secure-store'

import { parseStoredSession, type TokenRead } from './storedSession'

export { parseStoredSession, type TokenRead } from './storedSession'

/**
 * Native session storage: iOS Keychain / Android Keystore-backed encryption.
 *
 * The session is a short-lived access token plus the rotating refresh token
 * the Core issues to the mobile client (POST /auth/refresh). Only the refresh
 * token keeps someone signed in across restarts; both are stored together.
 *
 * AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY: readable while the phone is locked (so
 * resuming the app from a locked screen never loses the session), still never
 * copied into backups or onto another device. (Web resolves tokenStorage.web.ts.)
 */
const SESSION_KEY = 'trindatron.session.v2'
const LEGACY_TOKEN_KEY = 'trindatron.session.token'
const OPTIONS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
}

/**
 * "unavailable" means the secure store could not be read right now — it is
 * NOT a signed-out state, and the caller must retry instead of logging out.
 */
export async function readToken(): Promise<TokenRead> {
  try {
    const session = parseStoredSession(await SecureStore.getItemAsync(SESSION_KEY, OPTIONS))
    if (session) return { status: 'found', ...session }
    // A session saved before refresh tokens existed.
    const legacy = await SecureStore.getItemAsync(LEGACY_TOKEN_KEY, OPTIONS)
    return legacy ? { status: 'found', token: legacy, refreshToken: null } : { status: 'absent' }
  } catch {
    return { status: 'unavailable' }
  }
}

export async function writeToken(token: string, refreshToken: string | null = null): Promise<void> {
  await SecureStore.setItemAsync(SESSION_KEY, JSON.stringify({ access: token, refresh: refreshToken }), OPTIONS)
  await SecureStore.deleteItemAsync(LEGACY_TOKEN_KEY, OPTIONS).catch(() => undefined)
}

export async function deleteToken(): Promise<void> {
  await SecureStore.deleteItemAsync(SESSION_KEY, OPTIONS).catch(() => undefined)
  await SecureStore.deleteItemAsync(LEGACY_TOKEN_KEY, OPTIONS).catch(() => undefined)
}
