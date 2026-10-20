import { useLocaleStore, type LanguagePreference } from '@/i18n'
import { getPreferences, updatePreferences } from '@/services/api/settings'

/**
 * One language for the app and its answers.
 *
 * There used to be two controls that never met: Profile changed the app's own text (stored on the phone), Settings
 * changed the language the Core answers in (stored on the account). Choosing English in Profile left the answers in
 * Portuguese. Both now call chooseLanguage, which changes the app at once and records the same choice on the account,
 * where the Web reads it too.
 *
 * "Automatic" means the phone's language. The Core cannot see the phone, so every message carries the language the
 * app is showing (services/api/sessions.ts) and the Core answers in it unless a language was chosen.
 */
export async function chooseLanguage(preference: LanguagePreference): Promise<void> {
  await useLocaleStore.getState().setPreference(preference)
  try {
    await updatePreferences({ language: preference })
  } catch {
    // The app already shows the choice, and every message carries it, so answers follow it on this phone. The account
    // catches up the next time the app starts signed in (syncLanguageWithAccount).
  }
}

/**
 * After signing in: the account's choice and the phone's agree.
 *
 * A language chosen on the account (here, or on the Web) wins. When the account is still on "automatic" but this phone
 * has a choice — made before the two were joined, or while the Core could not be reached — the phone's choice is
 * recorded on the account instead of being thrown away.
 */
export async function syncLanguageWithAccount(signal?: AbortSignal): Promise<void> {
  const account = (await getPreferences(signal)).language
  const local = useLocaleStore.getState().preference
  if (account === local) return
  if (account === 'auto') {
    await updatePreferences({ language: local })
    return
  }
  await useLocaleStore.getState().setPreference(account)
}
