import AsyncStorage from '@react-native-async-storage/async-storage'

/**
 * Minimal offline cache: the last known profile and conversation list, so the
 * app opens with something useful on a flaky connection. Never secrets, never
 * message bodies. Wiped on sign-out. The Core remains the source of truth.
 */
const PREFIX = 'trindatron.cache.'

export type CacheKey = 'user' | 'sessions' | 'projects'

export async function readCache<T>(key: CacheKey): Promise<T | null> {
  try {
    const raw = await AsyncStorage.getItem(PREFIX + key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    return null
  }
}

export async function writeCache(key: CacheKey, value: unknown): Promise<void> {
  try {
    await AsyncStorage.setItem(PREFIX + key, JSON.stringify(value))
  } catch {
    // Cache is best-effort by definition.
  }
}

export async function clearCache(): Promise<void> {
  try {
    const keys = await AsyncStorage.getAllKeys()
    await AsyncStorage.multiRemove(keys.filter((key) => key.startsWith(PREFIX)))
  } catch {
    // Nothing else to do: the token itself is removed separately.
  }
}
