import { Platform } from 'react-native'

/**
 * Resolves the public TRINITY CORE base URL.
 *
 * Development falls back to the machine running Metro (Android emulators reach
 * it through 10.0.2.2). A release build without EXPO_PUBLIC_API_URL returns
 * null on purpose: guessing a production host would silently point users at
 * the wrong server, so the app shows a configuration error instead.
 */
export function resolveApiBaseUrl(value: string | undefined, isDev: boolean, os: string): string | null {
  const trimmed = value?.trim().replace(/\/+$/, '')
  if (trimmed) return trimmed
  if (!isDev) return null
  return os === 'android' ? 'http://10.0.2.2:8000' : 'http://localhost:8000'
}

// Must stay a literal `process.env.EXPO_PUBLIC_API_URL` access so Expo inlines it.
export const API_BASE_URL = resolveApiBaseUrl(process.env.EXPO_PUBLIC_API_URL, __DEV__, Platform.OS)
