/** Unique per attempt; used as the Core's Idempotency-Key and for local list keys. */
export function createId(): string {
  const cryptoApi = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID()
  const random = () => Math.random().toString(36).slice(2, 12)
  return `m-${Date.now().toString(36)}-${random()}${random()}`
}
