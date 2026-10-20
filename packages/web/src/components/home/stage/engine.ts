import * as THREE from 'three'
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js'
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js'
import { createAtmosphere, type Atmosphere } from './atmosphere'
import { createKnot, type Knot } from './knot'
import { createParticles, Formation, type Particles } from './particles'
import type { ChapterId } from './chapters'

/** 0 phones and weak devices, 1 tablets and ordinary laptops, 2 capable desktops. */
export type Tier = 0 | 1 | 2

export function deviceTier(): Tier {
  if (typeof window === 'undefined') return 1
  const nav = navigator as Navigator & { deviceMemory?: number; connection?: { saveData?: boolean } }
  const mem = nav.deviceMemory ?? 4
  const cores = nav.hardwareConcurrency ?? 4
  const small = Math.min(window.innerWidth, window.innerHeight) < 560
  const coarse = window.matchMedia('(pointer: coarse)').matches
  if (nav.connection?.saveData || mem <= 2 || cores <= 2 || (small && coarse)) return 0
  if (coarse || mem <= 4 || cores <= 4) return 1
  return 2
}

export function webglAvailable(): boolean {
  try {
    const c = document.createElement('canvas')
    return !!(c.getContext('webgl2') || c.getContext('webgl'))
  } catch {
    return false
  }
}

/** Where things sit for one chapter. x/y are fractions of the half-viewport; h is knot height / viewport height. */
interface Place { x: number; y: number; h: number; w: number; z?: number }
interface Look {
  land: Place
  port: Place
  formation: number
  /** Formation centre relative to the knot (world units), or fixed at the view's origin. */
  around: 'knot' | 'origin'
  a: string
  b: string
  top: string
  bottom: string
  tint: string
  fog: number
  spread: number
  opacity: number
  knotOpacity: number
  energy: number
  flow: number
  bloom: number
}

const CYAN = { a: '#2bd6e6', b: '#e6fbff' }
const base = { top: '#091014', bottom: '#020304', tint: '#1a8c9c', fog: 0.1 }

const hidden: Place = { x: 0, y: 0, h: 0, w: 0 }

const LOOKS: Record<ChapterId, Look> = {
  hero: { land: { x: 0.47, y: 0.02, h: 0.64, w: 0.37 }, port: { x: 0, y: 0.34, h: 0.3, w: 0.8 }, formation: Formation.nebula, around: 'knot', ...CYAN, ...base, spread: 1, opacity: 0.7, knotOpacity: 1, energy: 0.15, flow: 1, bloom: 0.85 },
  say: { land: { x: -0.5, y: 0, h: 0.46, w: 0.36 }, port: { x: 0, y: 0.5, h: 0.2, w: 0.55 }, formation: Formation.waves, around: 'knot', ...CYAN, ...base, tint: '#1596a8', spread: 1, opacity: 0.9, knotOpacity: 1, energy: 0.35, flow: 1.4, bloom: 0.9 },
  works: { land: { x: -0.5, y: 0, h: 0.4, w: 0.34 }, port: { x: 0, y: 0.5, h: 0.18, w: 0.5 }, formation: Formation.minds, around: 'knot', a: '#39e2f0', b: '#f4d9a6', ...base, tint: '#127f8f', spread: 1, opacity: 0.95, knotOpacity: 1, energy: 0.6, flow: 1.8, bloom: 0.95 },
  done: { land: { x: -0.5, y: 0, h: 0.5, w: 0.38 }, port: { x: 0, y: 0.5, h: 0.22, w: 0.6 }, formation: Formation.nebula, around: 'knot', ...CYAN, ...base, spread: 0.8, opacity: 0.9, knotOpacity: 1, energy: 0.9, flow: 1, bloom: 1.1 },
  film: { land: hidden, port: hidden, formation: Formation.dust, around: 'origin', ...CYAN, ...base, fog: 0.08, spread: 1, opacity: 0.5, knotOpacity: 0, energy: 0, flow: 1, bloom: 0.6 },
  // Creation: the knot steps aside for the work; a warm rose dust, the colour of what was made.
  create: { land: hidden, port: hidden, formation: Formation.dust, around: 'origin', a: '#ff9ec4', b: '#fff1f6', top: '#120a0e', bottom: '#030203', tint: '#8c3a5e', fog: 0.09, spread: 1.1, opacity: 0.6, knotOpacity: 0, energy: 0.1, flow: 0.8, bloom: 0.75 },
  today: { land: { x: 0.42, y: 0, h: 0.72, w: 0.5, z: -2 }, port: { x: 0, y: 0.1, h: 0.4, w: 0.9, z: -2 }, formation: Formation.nebula, around: 'knot', ...CYAN, ...base, spread: 1.2, opacity: 0.45, knotOpacity: 0.35, energy: 0.1, flow: 0.6, bloom: 0.6 },
  minds: { land: { x: 0.155, y: 0, h: 0.21, w: 0.14, z: -1 }, port: { x: 0, y: 0.58, h: 0.14, w: 0.4 }, formation: Formation.minds, around: 'knot', a: '#39e2f0', b: '#f4d9a6', ...base, tint: '#127f8f', spread: 0.9, opacity: 0.8, knotOpacity: 1, energy: 0.5, flow: 1.6, bloom: 0.9 },
  web: { land: { x: -0.16, y: 0, h: 0.21, w: 0.14, z: -1 }, port: { x: 0, y: 0.58, h: 0.14, w: 0.4 }, formation: Formation.globe, around: 'knot', a: '#3ab8ff', b: '#dff6ff', top: '#08131c', bottom: '#020406', tint: '#1c6fa6', fog: 0.13, spread: 0.85, opacity: 0.8, knotOpacity: 1, energy: 0.3, flow: 1.2, bloom: 0.9 },
  memory: { land: { x: -0.16, y: 0, h: 0.21, w: 0.14, z: -1 }, port: { x: 0, y: 0.58, h: 0.14, w: 0.4 }, formation: Formation.helix, around: 'knot', a: '#f2b45a', b: '#bff6ff', top: '#140f0a', bottom: '#040302', tint: '#a4691f', fog: 0.12, spread: 0.9, opacity: 0.8, knotOpacity: 1, energy: 0.3, flow: 0.8, bloom: 0.85 },
  files: { land: { x: -0.16, y: 0, h: 0.21, w: 0.14, z: -1 }, port: { x: 0, y: 0.58, h: 0.14, w: 0.4 }, formation: Formation.page, around: 'knot', a: '#9fd9ff', b: '#ffffff', top: '#0d1216', bottom: '#030405', tint: '#5a8aa6', fog: 0.1, spread: 0.85, opacity: 0.75, knotOpacity: 1, energy: 0.3, flow: 1, bloom: 0.8 },
  voice: { land: { x: 0.155, y: 0, h: 0.21, w: 0.14, z: -1 }, port: { x: 0, y: 0.58, h: 0.14, w: 0.4 }, formation: Formation.voice, around: 'knot', ...CYAN, ...base, tint: '#1596a8', spread: 0.8, opacity: 0.9, knotOpacity: 1, energy: 0.6, flow: 1.5, bloom: 1 },
  approval: { land: { x: 0.155, y: 0, h: 0.21, w: 0.14, z: -1 }, port: { x: 0, y: 0.58, h: 0.14, w: 0.4 }, formation: Formation.gate, around: 'knot', a: '#f2b45a', b: '#e6fbff', top: '#120f0b', bottom: '#030302', tint: '#8a5a1c', fog: 0.11, spread: 0.8, opacity: 0.85, knotOpacity: 1, energy: 0.2, flow: 0.7, bloom: 0.85 },
  // The studio opens with light rushing past; the work it does (slides, documents, computer, code) in calm cyan dust.
  build: { land: hidden, port: hidden, formation: Formation.dust, around: 'origin', ...CYAN, ...base, fog: 0.08, spread: 1.1, opacity: 0.55, knotOpacity: 0, energy: 0.1, flow: 0.9, bloom: 0.7 },
  studio: { land: hidden, port: hidden, formation: Formation.warp, around: 'origin', a: '#8feaf4', b: '#ffffff', top: '#05080b', bottom: '#010203', tint: '#1a8c9c', fog: 0.06, spread: 1, opacity: 0.9, knotOpacity: 0, energy: 0, flow: 1, bloom: 1 },
  try: { land: { x: -0.64, y: 0.42, h: 0.18, w: 0.14 }, port: { x: 0, y: 0.78, h: 0.1, w: 0.25 }, formation: Formation.dust, around: 'origin', ...CYAN, ...base, fog: 0.09, spread: 1, opacity: 0.55, knotOpacity: 1, energy: 0.3, flow: 1, bloom: 0.7 },
  final: { land: { x: 0, y: 0.42, h: 0.34, w: 0.34 }, port: { x: 0, y: 0.4, h: 0.22, w: 0.6 }, formation: Formation.nebula, around: 'knot', ...CYAN, ...base, fog: 0.14, spread: 1.1, opacity: 1, knotOpacity: 1, energy: 1, flow: 1.3, bloom: 1.1 },
}

const KNOT_WIDTH = 3
const CAMERA_Z = 10
const FOV = 35

/** Frame-rate independent approach to a target. */
const approach = (v: number, target: number, rate: number, dt: number) => v + (target - v) * (1 - Math.exp(-rate * dt))

export interface StageOptions {
  tier: Tier
  reducedMotion: boolean
  onReady?: () => void
}

export class StageEngine {
  private renderer: THREE.WebGLRenderer
  private composer: EffectComposer
  private bloom: UnrealBloomPass | null
  private scene = new THREE.Scene()
  private camera = new THREE.PerspectiveCamera(FOV, 1, 0.1, 120)
  private atmosphere: Atmosphere
  private particles: Particles
  private knot: Knot | null = null
  private raf = 0
  private last = 0
  private time = 0
  private running = false
  private visible = true
  private active = true

  private chapter: ChapterId = 'hero'
  private look: Look = LOOKS.hero
  // Smoothed, current values.
  private cur = { x: 0, y: 0, z: 0, scale: 0, opacity: 0, pOpacity: 0, spread: 1, energy: 0, flow: 1, bloom: 0.8, fog: 0.12 }
  private colors = {
    a: new THREE.Color(), b: new THREE.Color(), top: new THREE.Color(), bottom: new THREE.Color(), tint: new THREE.Color(),
  }
  private goal = {
    a: new THREE.Color(), b: new THREE.Color(), top: new THREE.Color(), bottom: new THREE.Color(), tint: new THREE.Color(),
  }
  private pointer = new THREE.Vector2(0, 0)
  private pointerTarget = new THREE.Vector2(0, 0)
  private pointerOn = 0
  private pointerSeen = false
  private yawDrag = 0
  private yawVel = 0
  private burst = 0
  private morphing = false
  private reduced: boolean
  // Adaptive quality: measured, not guessed. Slow frames step the stage down.
  private perf = { frames: 0, time: 0, level: 0, skip: false }
  private particleCount = 0

  static async create(canvas: HTMLCanvasElement, opts: StageOptions): Promise<StageEngine> {
    const engine = new StageEngine(canvas, opts)
    await engine.loadKnot()
    opts.onReady?.()
    return engine
  }

  private constructor(private canvas: HTMLCanvasElement, private opts: StageOptions) {
    this.reduced = opts.reducedMotion
    const tier = opts.tier
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: tier > 0, alpha: false, powerPreference: 'high-performance' })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, tier === 2 ? 1.75 : tier === 1 ? 1.4 : 1.15))
    this.renderer.outputColorSpace = THREE.SRGBColorSpace
    this.renderer.setClearColor(0x030405, 1)

    this.camera.position.set(0, 0, CAMERA_Z)
    this.atmosphere = createAtmosphere()
    this.scene.add(this.atmosphere.mesh)

    const count = tier === 2 ? 26000 : tier === 1 ? 14000 : 6500
    this.particles = createParticles(count, this.renderer.getPixelRatio())
    this.particleCount = count
    this.particles.uniforms.uSize.value = tier === 0 ? 2.6 : 2.2
    this.scene.add(this.particles.points)

    this.composer = new EffectComposer(this.renderer)
    this.composer.addPass(new RenderPass(this.scene, this.camera))
    this.bloom = tier > 0 ? new UnrealBloomPass(new THREE.Vector2(256, 256), 0.5, 0.5, 0.86) : null
    if (this.bloom) this.composer.addPass(this.bloom)
    this.composer.addPass(new OutputPass())

    this.applyColorsInstant(this.look)
    this.resize()
    this.snap()
  }

  private async loadKnot() {
    const tier = this.opts.tier
    this.knot = await createKnot({ width: KNOT_WIDTH, layers: tier === 2 ? 28 : tier === 1 ? 18 : 10, depth: 0.2 })
    this.scene.add(this.knot.group)
  }

  // ── public API ─────────────────────────────────────────────────────────

  start() {
    if (this.running) return
    this.running = true
    this.last = performance.now()
    const loop = (now: number) => {
      if (!this.running) return
      this.raf = requestAnimationFrame(loop)
      const real = (now - this.last) / 1000
      this.last = now
      // At the lowest level the stage draws every other frame: motion stays, the page stays responsive.
      if (this.perf.level >= 2 && (this.perf.skip = !this.perf.skip)) return
      this.frame(Math.min(0.05, real * (this.perf.level >= 2 ? 2 : 1)))
      this.measure(real)
    }
    this.raf = requestAnimationFrame(loop)
  }

  stop() {
    this.running = false
    cancelAnimationFrame(this.raf)
  }

  /** Pause when the page is hidden or an opaque chapter covers the stage. */
  setActive(active: boolean) {
    this.active = active
    this.sync()
  }

  setPageVisible(visible: boolean) {
    this.visible = visible
    this.sync()
  }

  private sync() {
    if (this.active && this.visible) this.start()
    else this.stop()
  }

  setChapter(id: ChapterId) {
    if (id === this.chapter) return
    this.chapter = id
    const next = LOOKS[id]
    const u = this.particles.uniforms
    if (next.formation !== u.uTo.value) {
      // Morph from wherever the cloud is now: if a morph is half done, start from its target.
      u.uFrom.value = u.uMix.value > 0.5 ? u.uTo.value : u.uFrom.value
      u.uTo.value = next.formation
      u.uMix.value = this.reduced ? 1 : 0
      this.morphing = !this.reduced
    }
    this.look = next
    this.setGoal(next)
    if (this.reduced) {
      this.applyColorsInstant(next)
      this.snap()
    }
    if (!this.running) this.frame(0)
  }

  /** Pointer in normalised device coordinates (-1..1, y up). */
  setPointer(nx: number, ny: number) {
    this.pointerTarget.set(nx, ny)
    this.pointerSeen = true
  }

  /** Horizontal drag in CSS pixels turns the knot; it eases back when released. */
  drag(dx: number) {
    this.yawVel = dx * 0.006
    this.yawDrag = THREE.MathUtils.clamp(this.yawDrag + dx * 0.006, -0.75, 0.75)
  }

  /** A tap wakes it: the light surges and a wave leaves the knot. */
  pulse() {
    this.burst = 1
    this.particles.uniforms.uPulse.value = this.time
    if (!this.running) this.frame(0)
  }

  /** The knot's centre and radius on screen (CSS px), for hit-testing taps. */
  knotOnScreen(): { x: number; y: number; r: number } | null {
    if (!this.knot || this.cur.scale < 0.05) return null
    const v = this.knot.group.position.clone().project(this.camera)
    const w = this.canvas.clientWidth
    const h = this.canvas.clientHeight
    const edge = this.knot.group.position.clone().add(new THREE.Vector3((KNOT_WIDTH * this.cur.scale) / 2, 0, 0)).project(this.camera)
    return { x: ((v.x + 1) / 2) * w, y: ((1 - v.y) / 2) * h, r: Math.abs(edge.x - v.x) * 0.5 * w }
  }

  /** Frames slower than ~28 fps over a second step the quality down: bloom off, then resolution and particles. */
  private measure(real: number) {
    if (this.perf.level >= 2 || real > 0.5) return // a long gap is a hidden tab, not a slow device
    this.perf.frames++
    this.perf.time += real
    if (this.perf.frames < 45) return
    const avg = this.perf.time / this.perf.frames
    this.perf.frames = 0
    this.perf.time = 0
    if (avg < 1 / 28) return
    this.perf.level++
    if (this.bloom) this.bloom.enabled = false
    const ratio = this.perf.level === 1 ? Math.min(1, this.renderer.getPixelRatio()) : 0.6
    this.renderer.setPixelRatio(ratio)
    this.composer.setPixelRatio(ratio)
    this.particles.uniforms.uPixelRatio.value = ratio
    this.particles.points.geometry.setDrawRange(0, Math.floor(this.particleCount / (this.perf.level === 1 ? 2 : 4)))
    this.resize()
  }

  resize() {
    const w = this.canvas.clientWidth || window.innerWidth
    const h = this.canvas.clientHeight || window.innerHeight
    this.renderer.setSize(w, h, false)
    this.composer.setSize(w, h)
    this.bloom?.setSize(Math.round(w / 2), Math.round(h / 2))
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
    this.atmosphere.uniforms.uRes.value.set(w, h)
    if (!this.running) this.frame(0)
  }

  dispose() {
    this.stop()
    this.knot?.dispose()
    this.particles.dispose()
    this.atmosphere.dispose()
    this.composer.dispose()
    this.renderer.dispose()
  }

  // ── internals ─────────────────────────────────────────────────────────

  private viewHalf() {
    const halfH = CAMERA_Z * Math.tan(THREE.MathUtils.degToRad(FOV / 2))
    return { halfH, halfW: halfH * this.camera.aspect }
  }

  private target() {
    const portrait = this.camera.aspect < 0.9
    const p = portrait ? this.look.port : this.look.land
    const { halfH, halfW } = this.viewHalf()
    const aspect = this.knot?.aspect ?? 1.06
    const knotH = KNOT_WIDTH / aspect
    const scale = p.h === 0 ? 0 : Math.min((p.h * 2 * halfH) / knotH, (p.w * 2 * halfW) / KNOT_WIDTH)
    return { x: p.x * halfW, y: p.y * halfH, z: p.z ?? 0, scale }
  }

  private snap() {
    const t = this.target()
    Object.assign(this.cur, {
      x: t.x, y: t.y, z: t.z, scale: t.scale,
      opacity: this.look.knotOpacity, pOpacity: this.look.opacity, spread: this.look.spread,
      energy: this.look.energy, flow: this.look.flow, bloom: this.look.bloom, fog: this.look.fog,
    })
  }

  private setGoal(look: Look) {
    const g = this.goal
    g.a.set(look.a); g.b.set(look.b); g.top.set(look.top); g.bottom.set(look.bottom); g.tint.set(look.tint)
  }

  private applyColorsInstant(look: Look) {
    this.setGoal(look)
    const c = this.colors
    const g = this.goal
    c.a.copy(g.a); c.b.copy(g.b); c.top.copy(g.top); c.bottom.copy(g.bottom); c.tint.copy(g.tint)
  }

  private frame(dt: number) {
    const motion = this.reduced ? 0 : 1
    this.time += dt * motion
    const t = this.time
    const look = this.look
    const target = this.target()
    const rate = 2.6

    // On a phone the stage sits behind scrolling text: outside the hero and the finale it becomes
    // ambient, so words are never fighting the symbol for the same pixels.
    const portrait = this.camera.aspect < 0.9
    const ambient = portrait && !(this.chapter === 'hero' || this.chapter === 'final' || this.chapter === 'try')
    const knotOpacity = ambient ? Math.min(look.knotOpacity, 0.26) : look.knotOpacity
    const partOpacity = ambient ? look.opacity * 0.5 : look.opacity

    const c = this.cur
    c.x = approach(c.x, target.x, rate, dt)
    c.y = approach(c.y, target.y, rate, dt)
    c.z = approach(c.z, target.z, rate, dt)
    c.scale = approach(c.scale, target.scale, rate, dt)
    c.opacity = approach(c.opacity, knotOpacity, rate, dt)
    c.pOpacity = approach(c.pOpacity, partOpacity, rate, dt)
    c.spread = approach(c.spread, look.spread, rate, dt)
    c.energy = approach(c.energy, look.energy, rate, dt)
    c.flow = approach(c.flow, look.flow, rate, dt)
    c.bloom = approach(c.bloom, look.bloom, rate, dt)
    c.fog = approach(c.fog, look.fog, rate, dt)

    const colorRate = 1 - Math.exp(-2.2 * dt)
    const ca = this.colors
    const g = this.goal
    ca.a.lerp(g.a, colorRate)
    ca.b.lerp(g.b, colorRate)
    ca.top.lerp(g.top, colorRate)
    ca.bottom.lerp(g.bottom, colorRate)
    ca.tint.lerp(g.tint, colorRate)

    // Pointer, drag and burst.
    this.pointer.x = approach(this.pointer.x, this.pointerTarget.x, 5, dt)
    this.pointer.y = approach(this.pointer.y, this.pointerTarget.y, 5, dt)
    this.pointerOn = approach(this.pointerOn, this.pointerSeen ? 1 : 0, 3, dt)
    this.yawVel *= Math.exp(-4 * dt)
    this.yawDrag = approach(this.yawDrag, 0, 0.9, dt)
    this.burst = approach(this.burst, 0, 1.6, dt)

    // Camera: a little parallax with the pointer.
    this.camera.position.x = approach(this.camera.position.x, this.pointer.x * 0.35 * motion, 2, dt)
    this.camera.position.y = approach(this.camera.position.y, this.pointer.y * 0.2 * motion, 2, dt)
    this.camera.lookAt(0, 0, 0)

    // The knot: floats, breathes, leans toward the pointer, turns when dragged.
    if (this.knot) {
      const g = this.knot.group
      const float = Math.sin(t * 0.6) * 0.07 * motion
      const breathe = 1 + Math.sin(t * 0.9) * 0.012 * motion + this.burst * 0.05
      g.position.set(c.x, c.y + float, c.z)
      g.scale.setScalar(Math.max(0.0001, c.scale * breathe))
      g.visible = c.scale > 0.01 && c.opacity > 0.01
      g.rotation.y = (Math.sin(t * 0.23) * 0.2 + Math.sin(t * 0.07) * 0.1) * motion + this.yawDrag + this.pointer.x * 0.28
      g.rotation.x = Math.sin(t * 0.31) * 0.05 * motion - this.pointer.y * 0.2
      g.rotation.z = Math.sin(t * 0.19) * 0.025 * motion
      const k = this.knot.uniforms
      k.uTime.value = t
      k.uEnergy.value = Math.min(1.6, c.energy + this.burst * 1.4)
      k.uFlow.value = c.flow
      k.uOpacity.value = c.opacity
      k.uLight.value.set(0.8 + this.pointer.x * 0.6, 0.6 + this.pointer.y * 0.6)
      // Pointer spotlight, in the knot's own uv space.
      const { halfH, halfW } = this.viewHalf()
      const px = this.pointer.x * halfW
      const py = this.pointer.y * halfH
      const size = KNOT_WIDTH * Math.max(0.001, c.scale)
      k.uPointer.value.set(0.5 + (px - c.x) / size, 0.5 + ((py - c.y) * this.knot.aspect) / size)
      k.uPointerOn.value = this.pointerOn
    }

    // Particles.
    const u = this.particles.uniforms
    u.uTime.value = t
    if (this.morphing) {
      u.uMix.value = Math.min(1, u.uMix.value + dt / 1.6)
      if (u.uMix.value >= 1) this.morphing = false
    }
    if (look.around === 'knot') u.uCenter.value.set(c.x, c.y, c.z - 0.2)
    else u.uCenter.value.set(0, 0, 0)
    u.uSpread.value = look.around === 'knot' ? c.spread * Math.max(0.55, Math.min(1.25, c.scale || 0.8)) : c.spread
    u.uOpacity.value = c.pOpacity
    u.uColorA.value.copy(ca.a)
    u.uColorB.value.copy(ca.b)

    const a = this.atmosphere.uniforms
    a.uTime.value = t
    a.uTop.value.copy(ca.top)
    a.uBottom.value.copy(ca.bottom)
    a.uTint.value.copy(ca.tint)
    a.uFog.value = c.fog
    a.uShaftX.value = 0.5 + (c.x / (this.viewHalf().halfW * 2)) * 0.9

    if (this.bloom) this.bloom.strength = c.bloom * 0.55
    this.composer.render(dt)
  }
}
