/**
 * Server-Sent Events reader for TRINITY CORE's `text/event-stream` responses.
 *
 * The Core emits `data: <json>\n\n` frames terminated by `data: [DONE]`, the
 * same protocol packages/web/src/api/client.ts parses. This implementation
 * follows the SSE spec a little more closely than the Web one (CRLF, multi-line
 * data, comments) so a proxy that re-frames the stream cannot break it.
 */

export const DONE_SENTINEL = '[DONE]'

export interface SSEParser {
  /** Feed decoded text. Returns true once the stream signalled completion. */
  push(chunk: string): boolean
  /** Flush a final frame that arrived without its trailing blank line. */
  end(): boolean
}

/** onData receives each frame's data payload; returning true stops the stream. */
export function createSSEParser(onData: (data: string) => boolean | void): SSEParser {
  let buffer = ''
  let dataLines: string[] = []
  let finished = false

  function dispatch(): boolean {
    if (dataLines.length === 0) return false
    const data = dataLines.join('\n')
    dataLines = []
    if (data.trim() === DONE_SENTINEL) return true
    return onData(data) === true
  }

  function processLine(line: string): boolean {
    if (line === '') return dispatch()
    if (line.startsWith(':')) return false
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    if (field !== 'data') return false
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    dataLines.push(value)
    return false
  }

  return {
    push(chunk) {
      if (finished) return true
      buffer += chunk
      let newline = buffer.search(/\r\n|\r|\n/)
      while (newline !== -1) {
        const line = buffer.slice(0, newline)
        const breakLength = buffer.startsWith('\r\n', newline) ? 2 : 1
        buffer = buffer.slice(newline + breakLength)
        if (processLine(line)) {
          finished = true
          return true
        }
        newline = buffer.search(/\r\n|\r|\n/)
      }
      return false
    },
    end() {
      if (finished) return true
      if (buffer !== '') {
        const rest = buffer
        buffer = ''
        if (processLine(rest)) return (finished = true)
      }
      finished = dispatch()
      return finished
    },
  }
}

export type StreamReadResult = 'completed' | 'ended' | 'aborted'

/**
 * Reads a streaming body to the end. 'completed' means the server said it was
 * done (or a handler stopped the stream); 'ended' means the connection closed
 * without that signal, which the caller must treat as an interrupted turn.
 */
export async function readEventStream(
  body: ReadableStream<Uint8Array> | null,
  onData: (data: string) => boolean | void,
  signal?: AbortSignal,
): Promise<StreamReadResult> {
  if (!body) return 'ended'
  const reader = body.getReader()
  const decoder = new TextDecoder()
  const parser = createSSEParser(onData)
  const onAbort = () => {
    reader.cancel().catch(() => undefined)
  }
  signal?.addEventListener('abort', onAbort)

  try {
    for (;;) {
      if (signal?.aborted) return 'aborted'
      const { done, value } = await reader.read()
      if (done) {
        return parser.end() ? 'completed' : signal?.aborted ? 'aborted' : 'ended'
      }
      if (parser.push(decoder.decode(value, { stream: true }))) {
        reader.cancel().catch(() => undefined)
        return 'completed'
      }
    }
  } catch (error) {
    if (signal?.aborted) return 'aborted'
    throw error
  } finally {
    signal?.removeEventListener('abort', onAbort)
  }
}
