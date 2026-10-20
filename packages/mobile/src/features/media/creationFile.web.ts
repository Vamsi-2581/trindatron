import { authHeaders, buildUrl } from '@/services/api/client'

import { creationFilename, type CreationAction } from './creationFileName'
import type { MediaCreation } from './media'

export { creationFilename, type CreationAction } from './creationFileName'

async function blobOf(media: MediaCreation, path: string | null | undefined): Promise<Blob> {
  if (!path) throw new Error('creation not ready')
  const response = await fetch(buildUrl(path), { headers: authHeaders(true) })
  if (!response.ok) throw new Error(`file request failed: ${response.status}`)
  return response.blob()
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

/** A real download of the file (the browser cannot follow a link that needs the session). */
export async function saveCreation(media: MediaCreation): Promise<CreationAction> {
  downloadBlob(await blobOf(media, media.downloadPath), creationFilename(media))
  return 'saved'
}

/** The Web Share API with the file itself where the browser supports it; otherwise the file is downloaded. */
export async function shareCreation(media: MediaCreation): Promise<CreationAction> {
  const blob = await blobOf(media, media.downloadPath)
  const filename = creationFilename(media)
  const file = new File([blob], filename, { type: blob.type || media.contentType || '' })
  if (typeof navigator.canShare === 'function' && navigator.canShare({ files: [file] })) {
    try {
      await navigator.share({ files: [file], title: 'TRINDATRON' })
      return 'shared'
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return 'cancelled'
    }
  }
  downloadBlob(blob, filename)
  return 'saved'
}

/** The browser cannot send the session with an <img> or <video>, so the viewer gets a local object URL. */
export async function creationViewSource(media: MediaCreation): Promise<{ uri: string; headers?: Record<string, string> }> {
  return { uri: URL.createObjectURL(await blobOf(media, media.contentPath)) }
}
