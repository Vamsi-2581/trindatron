import { Platform } from 'react-native'

import { t } from '@/i18n'
import { apiRequest } from '@/services/api/client'
import { ApiError } from '@/services/api/errors'
import type { AuthTokenResponse } from '@/services/api/types'

/**
 * "Continuar com Google" signs into the SAME TRINDATRON account system: Google
 * only proves the e-mail, the Core issues the TRINDATRON session.
 *
 * The Core verifies the ID token (signature against Google's keys, iss, exp,
 * nonce, verified e-mail) and requires its audience to be one of the app's own
 * OAuth client IDs. The button stays hidden until this build carries a client
 * ID for its platform — those come from the Google Cloud project (docs/MOBILE.md,
 * "Google"); none is invented here.
 */
export const CORE_GOOGLE_SIGN_IN_PATH: string | null = '/auth/google/id-token'

export interface GoogleClientIds {
  android: string | null
  ios: string | null
  web: string | null
}

const clean = (value: string | undefined) => (value && value.trim() ? value.trim() : null)

export function googleClientIds(): GoogleClientIds {
  // Literal process.env accesses so Expo inlines them. OAuth client IDs are public identifiers, not secrets.
  return {
    android: clean(process.env.EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID),
    ios: clean(process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID),
    web: clean(process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID),
  }
}

export function isGoogleSignInAvailable(
  os: string = Platform.OS,
  ids: GoogleClientIds = googleClientIds(),
  corePath: string | null = CORE_GOOGLE_SIGN_IN_PATH,
): boolean {
  if (!corePath) return false
  const id = os === 'android' ? ids.android : os === 'ios' ? ids.ios : ids.web
  return Boolean(id)
}

export async function exchangeGoogleIdToken(
  idToken: string,
  nonce: string,
  corePath: string | null = CORE_GOOGLE_SIGN_IN_PATH,
): Promise<AuthTokenResponse> {
  if (!corePath) {
    throw new ApiError({ kind: 'config', status: null, message: t('auth.google.failed') })
  }
  return apiRequest<AuthTokenResponse>(corePath, {
    method: 'POST',
    body: { id_token: idToken, nonce, client: 'mobile' },
    auth: false,
  })
}
