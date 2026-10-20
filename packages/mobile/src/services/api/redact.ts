/**
 * The user only ever meets TRINDATRON. Any text that reaches the screen from
 * the Core (errors, job output, agent errors) is checked for names of model
 * providers, models or internal infrastructure; if one slips through, a
 * neutral product message is shown instead. Nothing here changes routing.
 */
const INTERNAL_DETAIL = new RegExp(
  [
    'open\\s?ai', 'chat\\s?gpt', 'gpt[-\\s]?\\d', '\\bgpt\\b', 'o[1-4](-mini)?\\b',
    'anthropic', 'claude', 'sonnet', 'opus', 'haiku',
    'gemini', 'google\\s?ai', 'vertex', 'palm',
    'mistral', 'mixtral', 'codestral',
    'grok', '\\bx\\.?ai\\b', 'groq', 'llama', 'deepseek', 'qwen', 'cohere', 'perplexity',
    'eleven\\s?labs', 'litellm', 'ollama', 'bedrock', 'azure',
    'presenton',
    '\\bmodel[_\\s-]?(id|name|router|used)\\b', '\\bproviders?\\b', '\\bfallback\\b',
    '\\bapi[_\\s-]?key\\b', '\\b[A-Z]+_API_KEY\\b', 'traceback', 'exception',
  ].join('|'),
  'i',
)

export function containsInternalDetail(text: string): boolean {
  return INTERNAL_DETAIL.test(text)
}

export function safeUserMessage(message: string | null | undefined, fallback: string): string {
  const trimmed = message?.trim()
  return trimmed && !containsInternalDetail(trimmed) ? trimmed : fallback
}
