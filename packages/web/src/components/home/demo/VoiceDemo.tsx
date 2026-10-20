'use client'

import { useEffect, useRef, useState } from 'react'
import { Pause, Play } from 'lucide-react'
import { BrandSymbol } from '@/components/brand/master'
import { useLocale } from '../i18n'
import s from '../home.module.css'

const BARS = 48
/** Resting bar heights, rounded so server and browser render the same markup. */
const REST = Array.from({ length: BARS }, (_, i) => Math.round((0.1 + 0.5 * Math.abs(Math.sin(i * 0.55))) * 1000) / 1000)

/**
 * The real TRINDATRON voice: public/home/voice/voice-{lang}.mp3 was produced by
 * the product's own speech endpoint (POST /voice/synthesize), reading a real
 * answer word for word. The bars follow the audio itself.
 */
export function VoiceDemo() {
  const { t, lang } = useLocale()
  const v = t.voiceDemo
  const audio = useRef<HTMLAudioElement>(null)
  const bars = useRef<HTMLDivElement>(null)
  const analyser = useRef<AnalyserNode | null>(null)
  const ctx = useRef<AudioContext | null>(null)
  const raf = useRef(0)
  const [playing, setPlaying] = useState(false)
  const [text, setText] = useState('')
  const [progress, setProgress] = useState(0)

  useEffect(() => {
    let alive = true
    fetch(`/home/voice/voice-${lang}.json`).then((r) => r.json()).then((d: { text: string }) => { if (alive) setText(d.text) }).catch(() => {})
    setPlaying(false)
    setProgress(0)
    return () => { alive = false }
  }, [lang])

  useEffect(() => () => { cancelAnimationFrame(raf.current); ctx.current?.close().catch(() => {}) }, [])

  const draw = () => {
    const a = analyser.current
    const el = bars.current
    const au = audio.current
    if (!a || !el || !au) return
    const data = new Uint8Array(a.frequencyBinCount)
    a.getByteFrequencyData(data)
    const kids = el.children
    for (let i = 0; i < kids.length; i++) {
      const v2 = data[Math.floor((i / kids.length) * data.length * 0.7)] / 255
      ;(kids[i] as HTMLElement).style.transform = `scaleY(${0.08 + v2 * 0.92})`
    }
    setProgress(au.duration ? au.currentTime / au.duration : 0)
    raf.current = requestAnimationFrame(draw)
  }

  async function toggle() {
    const au = audio.current
    if (!au) return
    if (!playing) {
      try {
        if (!ctx.current) {
          const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
          ctx.current = new AC()
          const src = ctx.current.createMediaElementSource(au)
          analyser.current = ctx.current.createAnalyser()
          analyser.current.fftSize = 128
          src.connect(analyser.current)
          analyser.current.connect(ctx.current.destination)
        }
        await ctx.current.resume()
      } catch {
        // Without Web Audio the voice still plays; the bars just rest.
      }
      await au.play()
      setPlaying(true)
      raf.current = requestAnimationFrame(draw)
    } else {
      au.pause()
      setPlaying(false)
      cancelAnimationFrame(raf.current)
    }
  }

  return (
    <div className={s.voice}>
      <div className={s.voiceCard}>
        <BrandSymbol size={44} />
        <button type="button" className={s.voiceButton} onClick={toggle} aria-pressed={playing}>
          {playing ? <Pause size={18} aria-hidden="true" /> : <Play size={18} aria-hidden="true" />}
          {playing ? v.pause : v.play}
        </button>
        <div ref={bars} className={s.voiceBars} aria-hidden="true">
          {REST.map((v, i) => <span key={i} style={{ transform: `scaleY(${v})` }} />)}
        </div>
        <div className={s.voiceProgress} aria-hidden="true"><span style={{ transform: `scaleX(${progress})` }} /></div>
      </div>
      <p className={s.voiceLabel}>{v.transcriptLabel}</p>
      <blockquote className={s.voiceQuote}>{text}</blockquote>
      <audio
        ref={audio}
        src={`/home/voice/voice-${lang}.mp3`}
        preload="none"
        onEnded={() => { setPlaying(false); setProgress(1); cancelAnimationFrame(raf.current) }}
      />
      <p className={s.demoCaption}>{v.caption}</p>
    </div>
  )
}
