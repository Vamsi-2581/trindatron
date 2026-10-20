import { createSSEParser, readEventStream } from '../sse'

function streamOf(chunks: string[], close = true): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      if (close) controller.close()
    },
  })
}

describe('createSSEParser', () => {
  it('emits every frame even when the stream is split one character at a time', () => {
    const seen: string[] = []
    const parser = createSSEParser((data) => {
      seen.push(data)
    })
    const wire = 'data: {"type":"status"}\n\ndata: {"type":"text","content":"Olá"}\n\ndata: [DONE]\n\n'
    let completed = false
    for (const character of wire) completed = parser.push(character) || completed

    expect(seen).toEqual(['{"type":"status"}', '{"type":"text","content":"Olá"}'])
    expect(completed).toBe(true)
  })

  it('handles CRLF line endings, comments, other fields and multi-line data', () => {
    const seen: string[] = []
    const parser = createSSEParser((data) => {
      seen.push(data)
    })
    parser.push(': keep-alive\r\nevent: message\r\ndata: line one\r\ndata: line two\r\n\r\n')
    expect(seen).toEqual(['line one\nline two'])
  })

  it('stops as soon as the handler asks to', () => {
    const seen: string[] = []
    const parser = createSSEParser((data) => {
      seen.push(data)
      return data === 'stop'
    })
    expect(parser.push('data: stop\n\ndata: ignored\n\n')).toBe(true)
    expect(seen).toEqual(['stop'])
  })

  it('flushes a last frame that arrived without its blank line', () => {
    const seen: string[] = []
    const parser = createSSEParser((data) => {
      seen.push(data)
    })
    parser.push('data: tail')
    parser.end()
    expect(seen).toEqual(['tail'])
  })
})

describe('readEventStream', () => {
  it('reports completed when the server sends [DONE]', async () => {
    const seen: string[] = []
    const result = await readEventStream(streamOf(['data: a\n\n', 'data: [DONE]\n\n']), (data) => {
      seen.push(data)
    })
    expect(result).toBe('completed')
    expect(seen).toEqual(['a'])
  })

  it('reports ended when the connection closes without [DONE]', async () => {
    expect(await readEventStream(streamOf(['data: a\n\n']), () => undefined)).toBe('ended')
  })

  it('reports aborted when the caller cancels mid-stream', async () => {
    const controller = new AbortController()
    const result = await readEventStream(
      streamOf(['data: first\n\n'], false),
      () => {
        controller.abort()
      },
      controller.signal,
    )
    expect(result).toBe('aborted')
  })

  it('treats a missing body as ended', async () => {
    expect(await readEventStream(null, () => undefined)).toBe('ended')
  })
})
