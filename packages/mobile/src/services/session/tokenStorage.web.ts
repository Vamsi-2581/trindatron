import { parseStoredSession, type TokenRead } from './storedSession'

export { parseStoredSession, type TokenRead } from './storedSession'

/**
 * Web has no OS keychain, so the browser build keeps the session in
 * localStorage — the trade-off packages/mobile has always made on Web (see
 * docs/MOBILE.md, "Sessão"). The access token is short-lived; the refresh
 * token rotates on every use and a replayed one revokes the whole login.
 */
const SESSION_KEY = 'trindatron.session.v2'
const LEGACY_TOKEN_KEY = 'trindatron.session.token'

function storage(): Storage | null {
  return typeof window === 'undefined' ? null : window.localStorage
}

export async function readToken(): Promise<TokenRead> {
  try {
    const session = parseStoredSession(storage()?.getItem(SESSION_KEY) ?? null)
    if (session) return { status: 'found', ...session }
    const legacy = storage()?.getItem(LEGACY_TOKEN_KEY) ?? null
    return legacy ? { status: 'found', token: legacy, refreshToken: null } : { status: 'absent' }
  } catch {
    return { status: 'unavailable' }
  }
}

export async function writeToken(token: string, refreshToken: string | null = null): Promise<void> {
  storage()?.setItem(SESSION_KEY, JSON.stringify({ access: token, refresh: refreshToken }))
  storage()?.removeItem(LEGACY_TOKEN_KEY)
}

export async function deleteToken(): Promise<void> {
  try {
    storage()?.removeItem(SESSION_KEY)
    storage()?.removeItem(LEGACY_TOKEN_KEY)
  } catch {
    // Nothing else can be done in a locked-down browser.
  }
}
