import { useCallback, useEffect, useRef, useState } from 'react'

import { toApiError } from '@/services/api/errors'
import { uploadProjectFile } from '@/services/api/projects'
import type { ProjectFile } from '@/services/api/types'
import { createId } from '@/utils/ids'

import type { PickedFile } from './pickers'

export interface UploadItem {
  id: string
  projectId: string
  file: PickedFile
  progress: number
  status: 'uploading' | 'failed'
  error: string | null
}

/** Upload queue for the Core's project file storage: progress, cancel, retry. */
export function useProjectUploads(onUploaded: (file: ProjectFile) => void) {
  const [items, setItems] = useState<UploadItem[]>([])
  const itemsRef = useRef(items)
  const controllers = useRef(new Map<string, AbortController>())
  const onUploadedRef = useRef(onUploaded)

  useEffect(() => {
    itemsRef.current = items
    onUploadedRef.current = onUploaded
  })

  const run = useCallback(async (item: UploadItem) => {
    const controller = new AbortController()
    controllers.current.set(item.id, controller)
    try {
      const uploaded = await uploadProjectFile(item.projectId, item.file, {
        signal: controller.signal,
        onProgress: (progress) =>
          setItems((list) => list.map((entry) => (entry.id === item.id ? { ...entry, progress } : entry))),
      })
      setItems((list) => list.filter((entry) => entry.id !== item.id))
      onUploadedRef.current(uploaded)
    } catch (cause) {
      const error = toApiError(cause)
      setItems((list) =>
        error.kind === 'aborted'
          ? list.filter((entry) => entry.id !== item.id)
          : list.map((entry) => (entry.id === item.id ? { ...entry, status: 'failed', error: error.message } : entry)),
      )
    } finally {
      controllers.current.delete(item.id)
    }
  }, [])

  const start = useCallback(
    (projectId: string, file: PickedFile) => {
      const item: UploadItem = { id: createId(), projectId, file, progress: 0, status: 'uploading', error: null }
      setItems((list) => [...list, item])
      void run(item)
    },
    [run],
  )

  const cancel = useCallback((id: string) => controllers.current.get(id)?.abort(), [])

  const retry = useCallback(
    (id: string) => {
      const item = itemsRef.current.find((entry) => entry.id === id)
      if (!item) return
      const reset: UploadItem = { ...item, status: 'uploading', progress: 0, error: null }
      setItems((list) => list.map((entry) => (entry.id === id ? reset : entry)))
      void run(reset)
    },
    [run],
  )

  const dismiss = useCallback((id: string) => setItems((list) => list.filter((entry) => entry.id !== id)), [])

  useEffect(() => {
    const active = controllers.current
    return () => active.forEach((controller) => controller.abort())
  }, [])

  return { items, start, cancel, retry, dismiss }
}
