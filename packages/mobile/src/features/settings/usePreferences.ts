import { useCallback, useEffect, useRef, useState } from 'react'

import { useAsyncResource } from '@/hooks/useAsyncResource'
import { userMessage } from '@/services/api/errors'
import { getPreferences, updatePreferences, type Preferences, type PreferencesUpdate } from '@/services/api/settings'

interface PreferencesState {
  preferences: Preferences | null
  loading: boolean
  error: string | null
  saving: boolean
  save: (changes: PreferencesUpdate) => Promise<void>
  reload: () => Promise<void>
}

/**
 * The person's settings, with the Core as the only authority.
 *
 * Reading is the app's one loading pattern (useAsyncResource): same abort on
 * unmount, same refresh. Writing is optimistic and reversible — a switch moves
 * at once and goes back if the Core refuses, because a device that keeps a
 * preference the server rejected is a device that disagrees with the account.
 */
export function usePreferences(): PreferencesState {
  const resource = useAsyncResource((signal) => getPreferences(signal), [])
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const save = useCallback(
    async (changes: PreferencesUpdate) => {
      const previous = resource.data
      if (previous) resource.setData({ ...previous, ...changes })
      setSaving(true)
      setSaveError(null)
      try {
        const saved = await updatePreferences(changes)
        if (mounted.current) resource.setData(saved)
      } catch (cause) {
        if (mounted.current) {
          resource.setData(previous)
          setSaveError(userMessage(cause))
        }
      } finally {
        if (mounted.current) setSaving(false)
      }
    },
    [resource],
  )

  return {
    preferences: resource.data,
    loading: resource.loading,
    error: saveError ?? resource.error?.message ?? null,
    saving,
    save,
    reload: resource.refresh,
  }
}
