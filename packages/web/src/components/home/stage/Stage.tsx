'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ChapterId } from './chapters'
import type { StageEngine } from './engine'
import s from '../home.module.css'

type Mode = 'loading' | 'webgl' | 'fallback'

interface StageApi {
  mode: Mode
  setChapter: (id: ChapterId) => void
  pulse: () => void
}

const StageContext = createContext<StageApi>({ mode: 'loading', setChapter: () => {}, pulse: () => {} })
export const useStage = () => useContext(StageContext)

const INTERACTIVE = 'a, button, input, textarea, select, label, [role="button"], [data-no-stage]'

/**
 * The fixed WebGL stage behind the whole page. The engine (and three.js with
 * it) loads after first paint, so the words are readable before the light
 * arrives. Without WebGL the page keeps a still, CSS-lit version.
 */
export function StageProvider({ children }: { children: React.ReactNode }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const engine = useRef<StageEngine | null>(null)
  const pending = useRef<ChapterId>('hero')
  const [mode, setMode] = useState<Mode>('loading')
  const [lit, setLit] = useState(false)

  useEffect(() => {
    let cancelled = false
    let cleanup = () => {}
    ;(async () => {
      const mod = await import('./engine')
      if (cancelled || !canvas.current) return
      if (!mod.webglAvailable()) { setMode('fallback'); return }
      const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
      let e: StageEngine
      try {
        e = await mod.StageEngine.create(canvas.current, { tier: mod.deviceTier(), reducedMotion: reduced.matches })
      } catch {
        if (!cancelled) setMode('fallback')
        return
      }
      if (cancelled) { e.dispose(); return }
      engine.current = e
      e.setChapter(pending.current)
      e.start()
      setMode('webgl')
      requestAnimationFrame(() => setLit(true))

      // Pointer: lean and light toward it. Taps on the knot wake it; a drag turns it.
      let down: { x: number; y: number; t: number; onKnot: boolean; lastX: number } | null = null
      const nearKnot = (x: number, y: number) => {
        const k = e.knotOnScreen()
        return !!k && Math.hypot(x - k.x, y - k.y) < k.r * 1.05
      }
      const onMove = (ev: PointerEvent) => {
        if (ev.pointerType === 'mouse') e.setPointer((ev.clientX / window.innerWidth) * 2 - 1, 1 - (ev.clientY / window.innerHeight) * 2)
        if (down?.onKnot) {
          e.drag(ev.clientX - down.lastX)
          down.lastX = ev.clientX
        }
      }
      const onDown = (ev: PointerEvent) => {
        const target = ev.target as Element | null
        if (target?.closest(INTERACTIVE)) { down = null; return }
        down = { x: ev.clientX, y: ev.clientY, t: performance.now(), onKnot: nearKnot(ev.clientX, ev.clientY), lastX: ev.clientX }
      }
      const onUp = (ev: PointerEvent) => {
        if (down && down.onKnot && performance.now() - down.t < 350 && Math.hypot(ev.clientX - down.x, ev.clientY - down.y) < 10) e.pulse()
        down = null
      }
      const onResize = () => e.resize()
      const onVisibility = () => e.setPageVisible(document.visibilityState === 'visible')

      // Opaque chapters (the pink world, the pricing paper) cover the stage: stop drawing under them.
      const covers = () => Array.from(document.querySelectorAll<HTMLElement>('[data-stage-cover]'))
      let queued = false
      const checkCover = () => {
        queued = false
        const h = window.innerHeight
        const covered = covers().some((el) => {
          const r = el.getBoundingClientRect()
          return r.top <= 0 && r.bottom >= h
        })
        e.setActive(!covered)
      }
      const onScroll = () => {
        if (!queued) { queued = true; requestAnimationFrame(checkCover) }
      }

      window.addEventListener('pointermove', onMove, { passive: true })
      window.addEventListener('pointerdown', onDown, { passive: true })
      window.addEventListener('pointerup', onUp, { passive: true })
      window.addEventListener('resize', onResize)
      window.addEventListener('scroll', onScroll, { passive: true })
      document.addEventListener('visibilitychange', onVisibility)
      checkCover()
      cleanup = () => {
        window.removeEventListener('pointermove', onMove)
        window.removeEventListener('pointerdown', onDown)
        window.removeEventListener('pointerup', onUp)
        window.removeEventListener('resize', onResize)
        window.removeEventListener('scroll', onScroll)
        document.removeEventListener('visibilitychange', onVisibility)
        e.dispose()
        engine.current = null
      }
    })()
    return () => {
      cancelled = true
      cleanup()
    }
  }, [])

  const setChapter = useCallback((id: ChapterId) => {
    pending.current = id
    engine.current?.setChapter(id)
  }, [])
  const pulse = useCallback(() => engine.current?.pulse(), [])
  const api = useMemo(() => ({ mode, setChapter, pulse }), [mode, setChapter, pulse])

  return (
    <StageContext.Provider value={api}>
      <div className={s.stage} aria-hidden="true">
        <div className={s.stageAir} />
        <canvas ref={canvas} className={`${s.stageCanvas} ${lit ? s.stageLit : ''}`} />
      </div>
      {children}
    </StageContext.Provider>
  )
}
