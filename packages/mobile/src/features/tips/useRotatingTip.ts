import AsyncStorage from '@react-native-async-storage/async-storage'
import { useEffect, useRef, useState } from 'react'

import { pickTip, type Tip, type TipContext } from './tips'

export const TIP_STORAGE_KEY = 'trinity_last_tip'

/** One tip per visit, rotating across visits. Waits until the Core has answered what this user can do. */
export function useRotatingTip(context: TipContext, ready: boolean): Tip | null {
  const [tip, setTip] = useState<Tip | null>(null)
  const picked = useRef(false)
  const contextRef = useRef(context)

  useEffect(() => {
    contextRef.current = context
  })

  useEffect(() => {
    if (!ready || picked.current) return
    picked.current = true
    let cancelled = false
    void (async () => {
      let last: string | null = null
      try {
        last = await AsyncStorage.getItem(TIP_STORAGE_KEY)
      } catch {
        // Unreadable storage only means rotation starts over.
      }
      const next = pickTip(contextRef.current, last)
      if (cancelled) return
      setTip(next)
      if (next) {
        Promise.resolve()
          .then(() => AsyncStorage.setItem(TIP_STORAGE_KEY, next.id))
          .catch(() => undefined)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [ready])

  return tip
}
