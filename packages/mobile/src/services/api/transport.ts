import { fetch as expoFetch } from 'expo/fetch'

/**
 * The single network primitive. `expo/fetch` exposes a streaming
 * ReadableStream body on Android, iOS and Web, which React Native's built-in
 * fetch does not — that is what lets chat read the Core's SSE stream without a
 * polyfill or a second protocol.
 */

export interface TransportResponse {
  ok: boolean
  status: number
  headers: { get(name: string): string | null }
  text(): Promise<string>
  json(): Promise<unknown>
  arrayBuffer(): Promise<ArrayBuffer>
  body: ReadableStream<Uint8Array> | null
}

export interface TransportInit {
  method: string
  headers: Record<string, string>
  body?: string
  signal?: AbortSignal
}

export function transportFetch(url: string, init: TransportInit): Promise<TransportResponse> {
  return expoFetch(url, init) as unknown as Promise<TransportResponse>
}
