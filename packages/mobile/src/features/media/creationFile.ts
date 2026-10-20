import { Directory, File, Paths } from 'expo-file-system'
import * as Sharing from 'expo-sharing'
import { Platform } from 'react-native'

import { authHeaders, buildUrl } from '@/services/api/client'
import { authorizedSource } from '@/services/api/media'

import { creationFilename, type CreationAction } from './creationFileName'
import type { MediaCreation } from './media'

export { creationFilename, type CreationAction } from './creationFileName'

/** The finished file, fetched with the session into the app's cache (created files are readable by their owner only). */
async function cachedCopy(media: MediaCreation): Promise<File> {
  if (!media.downloadPath) throw new Error('creation not ready')
  const target = new File(Paths.cache, creationFilename(media))
  return File.downloadFileAsync(buildUrl(media.downloadPath), target, { headers: authHeaders(true), idempotent: true })
}

/** The system share sheet, with the file itself — not a link that only works while signed in. */
export async function shareCreation(media: MediaCreation): Promise<CreationAction> {
  const file = await cachedCopy(media)
  await Sharing.shareAsync(file.uri, { mimeType: media.contentType ?? undefined, dialogTitle: creationFilename(media) })
  return 'shared'
}

/**
 * Keeps a copy on the device. Android asks where (the system folder picker, no
 * storage permission needed); iOS saves from the share sheet ("Save Image",
 * "Save to Files"), which is where the system puts that choice.
 */
export async function saveCreation(media: MediaCreation): Promise<CreationAction> {
  const file = await cachedCopy(media)
  if (Platform.OS !== 'android') {
    await Sharing.shareAsync(file.uri, { mimeType: media.contentType ?? undefined })
    return 'shared'
  }
  let directory: Directory
  try {
    directory = await Directory.pickDirectoryAsync()
  } catch {
    return 'cancelled'
  }
  const saved = directory.createFile(creationFilename(media), media.contentType)
  saved.write(await file.bytes())
  return 'saved'
}

/** Where the viewer reads the file from: the Core path with the session. */
export async function creationViewSource(media: MediaCreation): Promise<{ uri: string; headers?: Record<string, string> }> {
  if (!media.contentPath) throw new Error('creation not ready')
  return authorizedSource(media.contentPath)
}
