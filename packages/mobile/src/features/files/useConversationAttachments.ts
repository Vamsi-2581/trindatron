import { useCallback, useEffect, useRef, useState } from 'react'

import { t } from '@/i18n'
import { attachUpload, listAttachments, removeAttachment, type SessionAttachment } from '@/services/api/attachments'
import { toApiError } from '@/services/api/errors'
import { createId } from '@/utils/ids'

import type { PickedFile } from './pickers'

/** The Core refuses anything larger (413); checking first avoids uploading a file only to have it refused. */
export const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

export interface PendingAttachment {
  id: string
  file: PickedFile
  progress: number
  status: 'uploading' | 'failed'
  error: string | null
}

/**
 * Files attached to one conversation, kept by the Core. A file only counts as
 * attached once the Core has stored it and answered with its id; until then it
 * is visibly uploading, and a refusal stays visible with a way to retry.
 * Readable documents become context for the next message.
 *
 * Before the first message (Home) there is no conversation yet: `ensureSession`
 * opens it when the first file is picked, so the files and the message land in
 * the same conversation.
 */
export function useConversationAttachments(sessionId: string | null, ensureSession?: () => Promise<string>) {
  const [attached, setAttached] = useState<SessionAttachment[]>([])
  const [pending, setPending] = useState<PendingAttachment[]>([])
  const [error, setError] = useState<string | null>(null)
  const controllers = useRef(new Map<string, AbortController>())

  useEffect(() => {
    if (!sessionId) return
    const controller = new AbortController()
    listAttachments(sessionId, controller.signal)
      .then((result) =>
        setAttached((current) => [...result.attachments, ...current.filter((entry) => !result.attachments.some((known) => known.id === entry.id))]),
      )
      .catch(() => undefined)
    return () => controller.abort()
  }, [sessionId])

  useEffect(() => {
    const active = controllers.current
    return () => active.forEach((controller) => controller.abort())
  }, [])

  const attach = useCallback(
    async (file: PickedFile) => {
      const id = createId()
      const fail = (message: string) =>
        setPending((list) => [...list.filter((entry) => entry.id !== id), { id, file, progress: 0, status: 'failed', error: message }])

      if (file.sizeBytes !== null && file.sizeBytes > MAX_ATTACHMENT_BYTES) {
        fail(t('error.code.too_large'))
        return
      }

      const controller = new AbortController()
      controllers.current.set(id, controller)
      setPending((list) => [...list, { id, file, progress: 0, status: 'uploading', error: null }])
      try {
        const target = sessionId ?? (ensureSession ? await ensureSession() : null)
        if (!target) throw new Error('attachments: no conversation to attach to')
        const created = await attachUpload(target, file, {
          signal: controller.signal,
          onProgress: (progress) => setPending((list) => list.map((entry) => (entry.id === id ? { ...entry, progress } : entry))),
        })
        setPending((list) => list.filter((entry) => entry.id !== id))
        setAttached((list) => [...list.filter((entry) => entry.id !== created.id), created])
      } catch (cause) {
        const failure = toApiError(cause)
        if (failure.kind === 'aborted') setPending((list) => list.filter((entry) => entry.id !== id))
        else fail(failure.message)
      } finally {
        controllers.current.delete(id)
      }
    },
    [sessionId, ensureSession],
  )

  const cancel = useCallback((id: string) => controllers.current.get(id)?.abort(), [])
  const dismiss = useCallback((id: string) => setPending((list) => list.filter((entry) => entry.id !== id)), [])
  const retry = useCallback(
    (item: PendingAttachment) => {
      dismiss(item.id)
      void attach(item.file)
    },
    [attach, dismiss],
  )

  const remove = useCallback(
    async (attachmentId: string) => {
      if (!sessionId) return
      setError(null)
      try {
        await removeAttachment(sessionId, attachmentId)
        setAttached((list) => list.filter((entry) => entry.id !== attachmentId))
      } catch (cause) {
        setError(toApiError(cause).message)
      }
    },
    [sessionId],
  )

  const uploading = pending.some((entry) => entry.status === 'uploading')
  return { attached, pending, uploading, error, attach, cancel, dismiss, retry, remove }
}
