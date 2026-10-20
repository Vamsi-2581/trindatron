import { File, Paths } from 'expo-file-system'
import * as Sharing from 'expo-sharing'

import { exportMyData } from '@/services/api/settings'

import { exportFilename } from './exportFilename'

export type ExportOutcome = 'shared' | 'cancelled'

export { exportFilename } from './exportFilename'

/**
 * The person's own data, handed to them as a file.
 *
 * Written to the app's cache and passed to the system share sheet — the same
 * route a finished creation takes (features/media/creationFile.ts), so "save to
 * Files", "send to myself" and every other destination the device offers are
 * the ones the system already provides. Nothing is uploaded anywhere, and the
 * file is exactly what the Core sent.
 */
export async function shareMyData(signal?: AbortSignal): Promise<ExportOutcome> {
  const body = await exportMyData(signal)
  const file = new File(Paths.cache, exportFilename())
  if (file.exists) file.delete()
  file.create()
  file.write(body)
  if (!(await Sharing.isAvailableAsync())) return 'cancelled'
  await Sharing.shareAsync(file.uri, { mimeType: 'application/json', dialogTitle: exportFilename() })
  return 'shared'
}
