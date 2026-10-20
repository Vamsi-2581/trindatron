'use client'

import { useEffect, useRef, useState, type ReactNode } from 'react'

/**
 * Reveal-on-scroll, and the page's only scroll listener.
 *
 * One IntersectionObserver per element, disconnected the moment it fires —
 * nothing keeps observing something already shown. Content is never hidden by
 * JavaScript that might not run: `.rv` applies the starting state and `.rv-in`
 * the finished one, and the reduced-motion block in globals.css neutralises
 * both, so a visitor who never triggers this still sees a complete page.
 */
export function Reveal({
  children,
  delay = 0,
  className = '',
  as: Tag = 'div',
}: {
  children: ReactNode
  /** ms, for staggering siblings. Keep under ~240 — beyond that it reads as lag. */
  delay?: number
  className?: string
  as?: 'div' | 'section' | 'li' | 'span'
}) {
  const ref = useRef<HTMLElement>(null)
  const [shown, setShown] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') {
      setShown(true)
      return
    }
    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return
        setShown(true)
        io.disconnect()
      },
      { rootMargin: '0px 0px -12% 0px', threshold: 0.08 },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [])

  return (
    <Tag
      ref={ref as never}
      className={`rv ${shown ? 'rv-in' : ''} ${className}`}
      style={shown && delay ? { transitionDelay: `${delay}ms` } : undefined}
    >
      {children}
    </Tag>
  )
}
