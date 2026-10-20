/**
 * The stored session's format, shared by the native and Web storage.
 *
 * It lives apart from tokenStorage on purpose: inside tokenStorage.web.ts, `./tokenStorage` resolves back to the
 * Web file itself, so importing these from there loaded nothing and every Web boot stopped at the opening screen.
 */
export type TokenRead =
  | { status: 'found'; token: string; refreshToken: string | null }
  | { status: 'absent' }
  | { status: 'unavailable' }

export function parseStoredSession(raw: string | null): { token: string; refreshToken: string | null } | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as { access?: unknown; refresh?: unknown }
    if (typeof value.access !== 'string' || !value.access) return null
    return { token: value.access, refreshToken: typeof value.refresh === 'string' && value.refresh ? value.refresh : null }
  } catch {
    return null
  }
}
