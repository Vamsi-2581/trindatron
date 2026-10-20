import { exportMyData } from '@/services/api/settings'

import { exportFilename } from './exportFilename'

export type ExportOutcome = 'shared' | 'cancelled'

export { exportFilename } from './exportFilename'

/** The browser cannot follow a link that needs the session, so the file is fetched and then downloaded. */
export async function shareMyData(signal?: AbortSignal): Promise<ExportOutcome> {
  const body = await exportMyData(signal)
  const url = URL.createObjectURL(new Blob([body], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = exportFilename()
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
  return 'shared'
}
