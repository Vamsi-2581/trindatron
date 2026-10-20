import { Platform } from 'react-native'

import type { UploadPart } from '@/services/api/upload'

/** The Core's /voice/transcribe allowlist accepts audio/mp4 (native .m4a) and audio/webm (browsers). */
export function recordingPart(uri: string, os: string = Platform.OS): UploadPart {
  return os === 'web'
    ? { uri, name: 'voice.webm', mimeType: 'audio/webm' }
    : { uri, name: 'voice.m4a', mimeType: 'audio/mp4' }
}
