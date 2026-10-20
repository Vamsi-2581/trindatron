import { useCallback, useEffect, useRef, useState } from 'react'

/** A flag that switches itself off after `durationMs` (e.g. "copiado"), with its timer cleaned up on unmount. */
export function useTransientFlag(durationMs = 2000): [boolean, () => void] {
  const [active, setActive] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const trigger = useCallback(() => {
    if (timer.current) clearTimeout(timer.current)
    setActive(true)
    timer.current = setTimeout(() => setActive(false), durationMs)
  }, [durationMs])

  return [active, trigger]
}
