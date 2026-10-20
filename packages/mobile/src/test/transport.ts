import type { TransportResponse } from '@/services/api/transport'

export function jsonResponse(status: number, body?: unknown, headers: Record<string, string> = {}): TransportResponse {
  const text = body === undefined ? '' : typeof body === 'string' ? body : JSON.stringify(body)
  const lowered = Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value]))
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => lowered[name.toLowerCase()] ?? null },
    text: async () => text,
    json: async () => JSON.parse(text),
    arrayBuffer: async () => new TextEncoder().encode(text).buffer as ArrayBuffer,
    body: null,
  }
}

export function sseResponse(frames: string[], options: { close?: boolean } = {}): TransportResponse {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      if (options.close !== false) controller.close()
    },
  })
  return { ...jsonResponse(200, undefined, { 'content-type': 'text/event-stream; charset=utf-8' }), body }
}

export const frame = (event: unknown) => `data: ${JSON.stringify(event)}\n\n`
