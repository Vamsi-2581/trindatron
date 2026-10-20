'use client'

import { useEffect, useState, type RefObject } from 'react'

/**
 * Plays a muted video only while it is on screen and pauses it when it leaves,
 * so a long page of films never decodes more than the one in view. With reduced
 * motion nothing starts on its own; `reduced` lets the caller offer a play button.
 * `source` re-arms the observer when the file changes (another language).
 */
export function useAutoplay(video: RefObject<HTMLVideoElement | null>, threshold = 0.35, source = '') {
  const [reduced, setReduced] = useState(false)
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const v = video.current
    if (!v) return
    const isReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    setReduced(isReduced)
    const io = new IntersectionObserver((entries) => {
      const on = entries.some((e) => e.isIntersecting)
      setVisible(on)
      if (on && !isReduced) v.play().catch(() => {})
      else v.pause()
    }, { threshold })
    io.observe(v)
    return () => io.disconnect()
  }, [video, threshold, source])

  return { reduced, visible }
}
