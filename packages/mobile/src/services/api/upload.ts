import { Platform } from 'react-native'

import { authHeaders, buildUrl, handleUnauthorized } from './client'
import { ApiError, errorFromResponse, fallbackMessage } from './errors'

/** A file chosen on the device. On Web, pickers also hand over the File itself. */
export interface UploadPart {
  uri: string
  name: string
  mimeType: string
  file?: Blob | null
}

export interface UploadOptions {
  fieldName?: string
  onProgress?: (fraction: number) => void
  signal?: AbortSignal
  timeoutMs?: number
}

const UPLOAD_TIMEOUT_MS = 120_000

async function appendPart(form: FormData, fieldName: string, part: UploadPart): Promise<void> {
  if (Platform.OS === 'web') {
    const blob = part.file ?? (await (await fetch(part.uri)).blob())
    form.append(fieldName, blob, part.name)
    return
  }
  // React Native's FormData streams the file from its URI natively.
  form.append(fieldName, { uri: part.uri, name: part.name, type: part.mimeType } as unknown as Blob)
}

/**
 * Multipart upload through XMLHttpRequest — the one API that reports upload
 * progress on Android, iOS and Web alike. Errors map exactly like apiRequest.
 */
export async function uploadMultipart<T>(path: string, part: UploadPart, options: UploadOptions = {}): Promise<T> {
  const url = buildUrl(path)
  const form = new FormData()
  await appendPart(form, options.fieldName ?? 'file', part)

  if (options.signal?.aborted) {
    throw new ApiError({ kind: 'aborted', status: null, message: fallbackMessage('aborted') })
  }

  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', url)
    xhr.timeout = options.timeoutMs ?? UPLOAD_TIMEOUT_MS
    for (const [name, value] of Object.entries(authHeaders(true))) xhr.setRequestHeader(name, value)
    xhr.setRequestHeader('Accept', 'application/json')

    const onAbortSignal = () => xhr.abort()
    options.signal?.addEventListener('abort', onAbortSignal)
    const settle = () => options.signal?.removeEventListener('abort', onAbortSignal)

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) options.onProgress?.(event.loaded / event.total)
    }
    xhr.onload = () => {
      settle()
      let body: unknown
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : undefined
      } catch {
        body = xhr.responseText
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        options.onProgress?.(1)
        resolve(body as T)
        return
      }
      const error = errorFromResponse(xhr.status, body)
      handleUnauthorized(error, true)
      reject(error)
    }
    xhr.onerror = () => {
      settle()
      reject(new ApiError({ kind: 'network', status: null, message: fallbackMessage('network') }))
    }
    xhr.ontimeout = () => {
      settle()
      reject(new ApiError({ kind: 'timeout', status: null, message: fallbackMessage('timeout') }))
    }
    xhr.onabort = () => {
      settle()
      reject(new ApiError({ kind: 'aborted', status: null, message: fallbackMessage('aborted') }))
    }
    xhr.send(form)
  })
}
