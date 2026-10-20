import type { MediaCreation } from './media'

export type CreationAction = 'saved' | 'shared' | 'cancelled'

const EXTENSIONS: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/webp': 'webp',
  'video/mp4': 'mp4',
  'video/webm': 'webm',
}

/** The same name Web gives a creation's file: trindatron-image-1a2b3c4d.png. */
export function creationFilename(media: Pick<MediaCreation, 'id' | 'kind' | 'contentType'>): string {
  const extension = EXTENSIONS[media.contentType ?? ''] ?? (media.kind === 'image' ? 'png' : 'mp4')
  return `trindatron-${media.kind}-${media.id.slice(0, 8)}.${extension}`
}
