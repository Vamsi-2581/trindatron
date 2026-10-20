import { font, space } from '@/theme'

/** Padding inside a cell, both sides — has to be part of the width or the text is clipped. */
const CELL_PADDING = space.sm * 2
/** Narrow enough for a column of numbers or "Sim"/"Não", wide enough to stay readable. */
export const MIN_COLUMN = 96
/** Past this a column wraps instead of growing, so one long cell cannot push the rest off screen. */
export const MAX_COLUMN = 260
/** Average glyph width of the body font. The table only needs proportions, not typography-exact metrics. */
const CHAR_WIDTH = font.size.md * 0.58

/**
 * One width per column, shared by the header and every row.
 *
 * React Native has no table layout: each cell is laid out on its own, so a `minWidth`/`maxWidth` cell sizes to its own
 * text and the same column ends up a different width on every row — a table of plan limits came out as a staircase on
 * the phone. Widths are derived from the longest text in the column (not measured on screen) so they are identical for
 * every row from the first render, and stay stable while an answer is still streaming in.
 *
 * Rows may be shorter than the header (a model can emit a ragged table); the column count covers the longest row.
 */
export function tableColumnWidths(rows: string[][]): number[] {
  const columns = rows.reduce((max, row) => Math.max(max, row.length), 0)
  return Array.from({ length: columns }, (_, column) => {
    const longest = rows.reduce((max, row) => Math.max(max, row[column]?.trim().length ?? 0), 0)
    return Math.min(MAX_COLUMN, Math.max(MIN_COLUMN, Math.round(longest * CHAR_WIDTH) + CELL_PADDING))
  })
}
