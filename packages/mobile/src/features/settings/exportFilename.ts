/** trindatron-dados-2026-09-16.json — one name, shared by the native and web exports. */
export function exportFilename(now = new Date()): string {
  return `trindatron-dados-${now.toISOString().slice(0, 10)}.json`
}
