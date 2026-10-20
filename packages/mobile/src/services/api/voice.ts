import { openRequest } from './client'
import { uploadMultipart, type UploadPart } from './upload'
import type { VoiceTranscription } from './types'

/** Speech-to-text through the Core. The provider and its key never leave the server. */
export function transcribeAudio(part: UploadPart, signal?: AbortSignal): Promise<VoiceTranscription> {
  return uploadMultipart<VoiceTranscription>('/voice/transcribe', part, { signal })
}

export interface SynthesizedSpeech {
  audio: ArrayBuffer
  /** What the Core returned: WAV from one provider, MPEG from another. */
  contentType: string
  /** The Core read only the start of the text — as much as one request of the plan allows (Core voice/budget.py). */
  shortened: boolean
}

export async function synthesizeSpeech(text: string, signal?: AbortSignal): Promise<SynthesizedSpeech> {
  const { response, dispose } = await openRequest('/voice/synthesize', {
    method: 'POST',
    body: { text },
    headers: { Accept: 'audio/*' },
    signal,
    timeoutMs: 60_000,
  })
  try {
    return {
      audio: await response.arrayBuffer(),
      contentType: (response.headers.get('content-type') ?? 'audio/mpeg').split(';')[0]!.trim(),
      shortened: response.headers.get('x-voice-truncated') === '1',
    }
  } finally {
    dispose()
  }
}
