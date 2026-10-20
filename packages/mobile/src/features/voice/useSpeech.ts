import { createAudioPlayer, setAudioModeAsync, type AudioPlayer } from 'expo-audio'
import { useCallback, useEffect, useRef, useState } from 'react'

import { userMessage } from '@/services/api/errors'
import { synthesizeSpeech } from '@/services/api/voice'

export type SpeechPhase = 'idle' | 'loading' | 'playing'

/**
 * Hearing an answer out loud.
 *
 * The Core synthesises (POST /voice/synthesize) and this plays what comes back.
 * Two rules keep it from becoming a mess of overlapping voices: only one
 * message speaks at a time — starting another stops the first — and the audio
 * for a given text is synthesised once and replayed from the same player, since
 * every synthesis is a real call that counts against the person's plan.
 */
interface ActiveSpeaker {
  stop: () => void
}

let active: ActiveSpeaker | null = null

export interface Speech {
  phase: SpeechPhase
  error: string | null
  /** Said when the Core read only the start of the answer: the rest is over what one request of the plan allows. */
  shortened: boolean
  /** Starts reading, or stops if this same text is already being read. */
  toggle: (text: string) => Promise<void>
  stop: () => void
}

function base64Of(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let index = 0; index < bytes.length; index += 1) binary += String.fromCharCode(bytes[index]!)
  return typeof btoa === 'function' ? btoa(binary) : Buffer.from(bytes).toString('base64')
}

export function useSpeech(): Speech {
  const [phase, setPhase] = useState<SpeechPhase>('idle')
  const [error, setError] = useState<string | null>(null)
  const [shortened, setShortened] = useState(false)
  const playerRef = useRef<AudioPlayer | null>(null)
  const spokenRef = useRef<string | null>(null)
  const mounted = useRef(true)
  // This hook's own handle in the single-speaker registry: identity, not a closure, so releasing
  // never has to compare functions with itself.
  const speaker = useRef<ActiveSpeaker>({ stop: () => undefined })

  const stop = useCallback(() => {
    playerRef.current?.pause()
    if (mounted.current) setPhase('idle')
  }, [])

  const release = useCallback(() => {
    playerRef.current?.remove()
    playerRef.current = null
    spokenRef.current = null
    if (active === speaker.current) active = null
  }, [])

  useEffect(() => {
    speaker.current.stop = stop
  }, [stop])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      // Leaving the screen must never leave a voice talking behind it.
      release()
    }
  }, [release])

  const toggle = useCallback(
    async (text: string) => {
      if (phase === 'playing' && spokenRef.current === text) {
        stop()
        return
      }
      // Another message was being read: that one stops before this one starts.
      if (active && active !== speaker.current) active.stop()
      setError(null)

      const player = playerRef.current
      if (player && spokenRef.current === text) {
        await setAudioModeAsync({ playsInSilentMode: true }).catch(() => undefined)
        await player.seekTo(0).catch(() => undefined)
        player.play()
        active = speaker.current
        setPhase('playing')
        return
      }

      setPhase('loading')
      setShortened(false)
      try {
        const speech = await synthesizeSpeech(text)
        if (!mounted.current) return
        release()
        await setAudioModeAsync({ playsInSilentMode: true }).catch(() => undefined)
        // The type the Core sent: one provider answers in WAV, another in MPEG.
        const created = createAudioPlayer({ uri: `data:${speech.contentType};base64,${base64Of(speech.audio)}` })
        // Back to "Ouvir" when the reading ends: without this the button stayed on "Ouvindo" after the audio was over.
        created.addListener('playbackStatusUpdate', (status) => {
          if (status.didJustFinish && mounted.current) setPhase('idle')
        })
        playerRef.current = created
        spokenRef.current = text
        setShortened(speech.shortened)
        created.play()
        active = speaker.current
        setPhase('playing')
      } catch (cause) {
        if (mounted.current) {
          setError(userMessage(cause))
          setPhase('idle')
        }
      }
    },
    [phase, release, stop],
  )

  return { phase, error, shortened, toggle, stop }
}
