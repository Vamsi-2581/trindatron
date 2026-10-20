import { t } from '@/i18n'

import { apiRequest } from './client'
import { ApiError } from './errors'
import type { AuthTokenResponse, User } from './types'

/** The app declares itself so the Core issues a rotating refresh token in the body. */
export const SESSION_CLIENT = 'mobile'

export async function login(email: string, password: string): Promise<AuthTokenResponse> {
  try {
    return await apiRequest<AuthTokenResponse>('/auth/login', {
      method: 'POST',
      body: { email, password, client: SESSION_CLIENT },
      auth: false,
    })
  } catch (error) {
    // The Core answers a bad credential with 401; on this call that is not an
    // expired session, and the message must not reveal which field was wrong.
    if (error instanceof ApiError && error.kind === 'unauthorized') {
      throw new ApiError({ kind: 'unauthorized', status: 401, message: t('auth.login.invalidCredentials') })
    }
    throw error
  }
}

export async function register(email: string, password: string, fullName: string): Promise<AuthTokenResponse> {
  try {
    return await apiRequest<AuthTokenResponse>('/auth/register', {
      method: 'POST',
      body: { email, password, full_name: fullName, client: SESSION_CLIENT },
      auth: false,
    })
  } catch (error) {
    if (error instanceof ApiError && error.kind === 'conflict') {
      throw new ApiError({ kind: 'conflict', status: 409, message: t('auth.register.emailTaken') })
    }
    throw error
  }
}

/** Exchange the refresh token for a new access token and a NEW refresh token (rotation). */
export function refreshSession(refreshToken: string): Promise<AuthTokenResponse> {
  return apiRequest<AuthTokenResponse>('/auth/refresh', {
    method: 'POST',
    body: { refresh_token: refreshToken },
    auth: false,
    retries: 0,
  })
}

/** Revoke this login on the server. Best effort: the local session ends either way. */
export function logout(refreshToken: string): Promise<void> {
  return apiRequest<void>('/auth/logout', { method: 'POST', body: { refresh_token: refreshToken }, auth: false, retries: 0 })
}

export function getMe(options: { signal?: AbortSignal; notifyUnauthorized?: boolean } = {}): Promise<User> {
  return apiRequest<User>('/auth/me', options)
}

export function updateTimezone(timezone: string): Promise<{ timezone: string | null }> {
  return apiRequest('/users/me/timezone', { method: 'PATCH', body: { timezone } })
}
