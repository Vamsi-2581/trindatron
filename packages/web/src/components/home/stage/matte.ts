/**
 * Alpha for the TRINDATRON master, derived at run time from the master's own
 * pixels. The master is never modified and no cut-out file exists: this only
 * returns a mask.
 *
 * "3. Modelo Vertical" sits on a graphite backdrop (max channel ~25-46) with a
 * drop shadow DARKER than the backdrop; the chrome starts around 55. Brightness
 * alone would punch holes in the chrome's dark grooves, so the backdrop is
 * found by connectivity:
 *   1. candidates are pixels whose max channel is below `floor`;
 *   2. a candidate region is backdrop if it touches the border (outer field,
 *      shadow, wordmark) or is wide enough to hold a disk of radius `core`
 *      (the holes inside the loops). Thin dark lines inside the chrome hold no
 *      such disk, so they stay part of the mark;
 *   3. JPEG grain clinging to the rim as hair-thin strands (weak, connected to
 *      the backdrop, away from bright chrome) is absorbed into the backdrop;
 *   4. only the largest piece survives (the knot), and its rim is feathered.
 */
export interface Matte {
  alpha: Uint8Array
  bbox: { x: number; y: number; w: number; h: number }
}

const INF = 1e9

function chamfer(dist: Float32Array, W: number, H: number): void {
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const i = y * W + x
    if (!dist[i]) continue
    let d = dist[i]
    if (x > 0) d = Math.min(d, dist[i - 1] + 1)
    if (y > 0) d = Math.min(d, dist[i - W] + 1)
    if (x > 0 && y > 0) d = Math.min(d, dist[i - W - 1] + 1.414)
    if (x < W - 1 && y > 0) d = Math.min(d, dist[i - W + 1] + 1.414)
    dist[i] = d
  }
  for (let y = H - 1; y >= 0; y--) for (let x = W - 1; x >= 0; x--) {
    const i = y * W + x
    if (!dist[i]) continue
    let d = dist[i]
    if (x < W - 1) d = Math.min(d, dist[i + 1] + 1)
    if (y < H - 1) d = Math.min(d, dist[i + W] + 1)
    if (x < W - 1 && y < H - 1) d = Math.min(d, dist[i + W + 1] + 1.414)
    if (x > 0 && y < H - 1) d = Math.min(d, dist[i + W - 1] + 1.414)
    dist[i] = d
  }
}

export function computeMatte(
  rgba: Uint8ClampedArray | Uint8Array,
  W: number,
  H: number,
  { floor = 48, core = 5, feather = 16 } = {},
): Matte {
  const N = W * H
  const maxc = new Uint8Array(N)
  const cand = new Uint8Array(N)
  for (let i = 0; i < N; i++) {
    const m = Math.max(rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2])
    maxc[i] = m
    cand[i] = m < floor ? 1 : 0
  }

  const dist = new Float32Array(N)
  for (let i = 0; i < N; i++) dist[i] = cand[i] ? INF : 0
  chamfer(dist, W, H)

  const backdrop = new Uint8Array(N)
  const seen = new Uint8Array(N)
  const stack = new Int32Array(N)
  const members = new Int32Array(N)
  for (let s = 0; s < N; s++) {
    if (!cand[s] || seen[s]) continue
    let top = 0
    let count = 0
    let isBackdrop = false
    stack[top++] = s
    seen[s] = 1
    while (top) {
      const i = stack[--top]
      members[count++] = i
      const x = i % W
      const y = (i / W) | 0
      if (x === 0 || y === 0 || x === W - 1 || y === H - 1 || dist[i] >= core) isBackdrop = true
      if (x > 0 && cand[i - 1] && !seen[i - 1]) { seen[i - 1] = 1; stack[top++] = i - 1 }
      if (x < W - 1 && cand[i + 1] && !seen[i + 1]) { seen[i + 1] = 1; stack[top++] = i + 1 }
      if (y > 0 && cand[i - W] && !seen[i - W]) { seen[i - W] = 1; stack[top++] = i - W }
      if (y < H - 1 && cand[i + W] && !seen[i + W]) { seen[i + W] = 1; stack[top++] = i + W }
    }
    if (isBackdrop) for (let k = 0; k < count; k++) backdrop[members[k]] = 1
  }

  // Hair-thin grain on the rim: weak, connected to the backdrop, away from bright chrome.
  const strongGap = 12
  const reach = 2
  const dStrong = new Float32Array(N)
  for (let i = 0; i < N; i++) dStrong[i] = maxc[i] >= floor + strongGap ? 0 : INF
  chamfer(dStrong, W, H)
  {
    let top = 0
    for (let i = 0; i < N; i++) if (backdrop[i]) stack[top++] = i
    while (top) {
      const i = stack[--top]
      const x = i % W
      const y = (i / W) | 0
      const next = [x > 0 ? i - 1 : -1, x < W - 1 ? i + 1 : -1, y > 0 ? i - W : -1, y < H - 1 ? i + W : -1]
      for (const j of next) {
        if (j < 0 || backdrop[j]) continue
        if (maxc[j] < floor + strongGap && dStrong[j] > reach) { backdrop[j] = 1; stack[top++] = j }
      }
    }
  }

  const alpha = new Uint8Array(N)
  for (let i = 0; i < N; i++) {
    if (backdrop[i]) continue
    const x = i % W
    const y = (i / W) | 0
    let rim = false
    for (let dy = -2; dy <= 2 && !rim; dy++) for (let dx = -2; dx <= 2; dx++) {
      const xx = x + dx
      const yy = y + dy
      if (xx < 0 || yy < 0 || xx >= W || yy >= H) continue
      if (backdrop[yy * W + xx]) { rim = true; break }
    }
    if (!rim) { alpha[i] = 255; continue }
    const t = Math.min(1, Math.max(0, (maxc[i] - floor - 2) / feather))
    alpha[i] = Math.round(255 * t * t * (3 - 2 * t))
  }

  // Keep only the largest piece: the knot. Backdrop grain and the wordmark are islands.
  const lab = new Int32Array(N).fill(-1)
  let best = -1
  let bestSize = 0
  let nLab = 0
  for (let s = 0; s < N; s++) {
    if (!alpha[s] || lab[s] !== -1) continue
    let top = 0
    let size = 0
    stack[top++] = s
    lab[s] = nLab
    while (top) {
      const i = stack[--top]
      size++
      const x = i % W
      const y = (i / W) | 0
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const xx = x + dx
        const yy = y + dy
        if ((!dx && !dy) || xx < 0 || yy < 0 || xx >= W || yy >= H) continue
        const j = yy * W + xx
        if (alpha[j] && lab[j] === -1) { lab[j] = nLab; stack[top++] = j }
      }
    }
    if (size > bestSize) { bestSize = size; best = nLab }
    nLab++
  }
  for (let i = 0; i < N; i++) if (lab[i] !== best) alpha[i] = 0

  let x0 = W
  let y0 = H
  let x1 = 0
  let y1 = 0
  for (let i = 0; i < N; i++) {
    if (alpha[i] <= 8) continue
    const x = i % W
    const y = (i / W) | 0
    if (x < x0) x0 = x
    if (x > x1) x1 = x
    if (y < y0) y0 = y
    if (y > y1) y1 = y
  }
  return { alpha, bbox: { x: x0, y: y0, w: x1 - x0 + 1, h: y1 - y0 + 1 } }
}
