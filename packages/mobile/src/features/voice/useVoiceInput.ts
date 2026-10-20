import {
  RecordingPresets,
  requestRecordingPermissionsAsync,
  setAudioModeAsync,
  useAudioRecorder,
  useAudioRecorderState,
} from 'expo-audio'
import { useCallback, useState } from 'react'

import { t } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { transcribeAudio } from '@/services/api/voice'

import { recordingPart } from './recordingPart'

export type VoicePhase = 'idle' | 'recording' | 'transcribing'

export function useVoiceInput(onTranscribed: (text: string) => void) {
  const recorder = useAudioRecorder(RecordingPresets.HIGH_QUALITY)
  const recorderState = useAudioRecorderState(recorder, 250)
  const [phase, setPhase] = useState<VoicePhase>('idle')
  const [error, setError] = useState<string | null>(null)

  const start = useCallback(async () => {
    setError(null)
    const permission = await requestRecordingPermissionsAsync()
    if (!permission.granted) {
      setError(t('voice.permissionDenied'))
      return
    }
    try {
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true })
      await recorder.prepareToRecordAsync()
      recorder.record()
      setPhase('recording')
    } catch {
      setError(t('voice.startFailed'))
      setPhase('idle')
    }
  }, [recorder])

  const stop = useCallback(async () => {
    try {
      await recorder.stop()
    } catch {
      // Stopping an already-stopped recorder is harmless.
    }
    await setAudioModeAsync({ allowsRecording: false }).catch(() => undefined)
    const uri = recorder.uri
    if (!uri) {
      setPhase('idle')
      setError(t('voice.empty'))
      return
    }
    setPhase('transcribing')
    try {
      const result = await transcribeAudio(recordingPart(uri))
      const text = result.text.trim()
      if (text) onTranscribed(text)
      else setError(t('voice.notUnderstood'))
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setPhase('idle')
    }
  }, [recorder, onTranscribed])

  /** Discards the recording: nothing is uploaded or transcribed. */
  const cancel = useCallback(async () => {
    try {
      await recorder.stop()
    } catch {
      // Already stopped.
    }
    await setAudioModeAsync({ allowsRecording: false }).catch(() => undefined)
    setPhase('idle')
  }, [recorder])

  return {
    phase,
    error,
    durationMs: recorderState.durationMillis,
    start,
    stop,
    cancel,
    clearError: () => setError(null),
  }
}
