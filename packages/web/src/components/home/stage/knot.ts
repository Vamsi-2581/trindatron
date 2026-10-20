import * as THREE from 'three'
import { computeMatte } from './matte'

/**
 * The TRINDATRON symbol as a living object.
 *
 * Source: public/brand/master/source/trindatron-lockup-vertical.jpg, a
 * byte-identical copy of "IMAGENS LOGO/3. Modelo Vertical" — the master the
 * brand folder designates for the hero. Its pixels are never edited or
 * redrawn: the backdrop is dropped at load time by a mask derived from the
 * master itself (matte.ts), in memory only. Everything alive about it is
 * drawn on top:
 *  - volume: dark layers behind the face, shaped by the master's silhouette
 *  - energy: the master's own cyan channel carries light around the lobes
 *  - chrome: a specular band sweeps the metal, and the pointer lights it
 */

export const MASTER_URL = '/brand/master/source/trindatron-lockup-vertical.jpg'
/** Where the symbol sits in the master (px), with room for the matte to see backdrop around it. */
const REGION = { x: 174, y: 215, w: 640, h: 608 }
const PAD = 10

const vert = /* glsl */ `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
`

// The texture is sampled without colour conversion: texture2D returns the
// master's own sRGB bytes on every GPU. The result is converted to linear for
// the composer, whose OutputPass encodes it back: the art comes out as it went in.
const faceFrag = /* glsl */ `
  uniform sampler2D uTex;
  uniform float uTime;
  uniform float uEnergy;
  uniform float uFlow;
  uniform vec2 uLight;
  uniform vec2 uPointer;
  uniform float uPointerOn;
  uniform float uOpacity;
  varying vec2 vUv;
  vec3 toLinear(vec3 s) { return pow(max(s, vec3(0.0)), vec3(2.2)); }
  void main() {
    vec4 t = texture2D(uTex, vUv);
    if (t.a < 0.01) discard;
    vec3 c = t.rgb;
    float mx = max(c.r, max(c.g, c.b));
    float mn = min(c.r, min(c.g, c.b));
    float sat = mx > 0.0 ? (mx - mn) / mx : 0.0;
    float cyan = smoothstep(0.38, 0.62, sat) * smoothstep(0.10, 0.22, c.b - c.r) * smoothstep(0.10, 0.22, c.g - c.r);
    float lum = dot(c, vec3(0.299, 0.587, 0.114));

    // Light travelling around the three lobes, from the knot's central crossing.
    vec2 p = vUv - vec2(0.48, 0.56);
    float ang = atan(p.y, p.x);
    float r = length(p);
    float flow = pow(0.5 + 0.5 * sin(ang * 3.0 - uTime * 2.2 * uFlow + r * 16.0), 8.0);
    float flow2 = pow(0.5 + 0.5 * sin(-ang * 3.0 - uTime * 1.2 * uFlow + r * 9.0), 14.0);
    float ring = exp(-pow((r - fract(uTime * 0.35) * 0.8) * 18.0, 2.0)) * uEnergy;
    vec3 emissive = vec3(0.22, 0.92, 1.0) * cyan * (0.25 + 1.3 * flow + 0.6 * flow2 + 2.2 * ring) * (0.85 + 0.9 * uEnergy);

    float metal = (1.0 - cyan) * smoothstep(0.30, 0.85, lum);
    float band = exp(-pow((dot(vUv - 0.5, normalize(uLight + vec2(0.0001))) - (fract(uTime * 0.07) * 2.6 - 1.3)) * 7.0, 2.0));
    float spot = exp(-dot(vUv - uPointer, vUv - uPointer) * 22.0) * uPointerOn;

    vec3 col = toLinear(c) + emissive + metal * (band * 0.24 + spot * 0.2) * vec3(0.92, 0.98, 1.0);
    gl_FragColor = vec4(col, t.a * uOpacity);
  }
`

const layerFrag = /* glsl */ `
  uniform sampler2D uTex;
  uniform float uDepth;
  uniform float uOpacity;
  varying vec2 vUv;
  void main() {
    if (texture2D(uTex, vUv).a < 0.6 || uOpacity < 0.5) discard;
    float shade = mix(0.16, 0.03, uDepth);
    gl_FragColor = vec4(shade * 0.88, shade * 0.97, shade * 1.06, 1.0);
  }
`

async function loadMasterSymbol(): Promise<{ canvas: HTMLCanvasElement; w: number; h: number }> {
  const img = new Image()
  img.decoding = 'async'
  img.src = MASTER_URL
  await img.decode()
  const src = document.createElement('canvas')
  src.width = REGION.w
  src.height = REGION.h
  const sctx = src.getContext('2d', { willReadFrequently: true })!
  sctx.drawImage(img, REGION.x, REGION.y, REGION.w, REGION.h, 0, 0, REGION.w, REGION.h)
  const data = sctx.getImageData(0, 0, REGION.w, REGION.h)
  const { alpha, bbox } = computeMatte(data.data, REGION.w, REGION.h)

  const x0 = Math.max(0, bbox.x - PAD)
  const y0 = Math.max(0, bbox.y - PAD)
  const w = Math.min(REGION.w - x0, bbox.w + 2 * PAD)
  const h = Math.min(REGION.h - y0, bbox.h + 2 * PAD)
  const out = document.createElement('canvas')
  out.width = w
  out.height = h
  const octx = out.getContext('2d')!
  const img2 = octx.createImageData(w, h)
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y + y0) * REGION.w + (x + x0)
      const o = (y * w + x) * 4
      img2.data[o] = data.data[i * 4]
      img2.data[o + 1] = data.data[i * 4 + 1]
      img2.data[o + 2] = data.data[i * 4 + 2]
      img2.data[o + 3] = alpha[i]
    }
  }
  octx.putImageData(img2, 0, 0)
  return { canvas: out, w, h }
}

export interface Knot {
  group: THREE.Group
  uniforms: {
    uTime: { value: number }
    uEnergy: { value: number }
    uFlow: { value: number }
    uLight: { value: THREE.Vector2 }
    uPointer: { value: THREE.Vector2 }
    uPointerOn: { value: number }
    uOpacity: { value: number }
  }
  /** Width / height of the symbol. */
  aspect: number
  dispose: () => void
}

/** `width` is the knot's width in world units. */
export async function createKnot({ width = 3, layers = 16, depth = 0.2 } = {}): Promise<Knot> {
  const { canvas, w, h } = await loadMasterSymbol()
  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.NoColorSpace
  tex.premultiplyAlpha = false
  tex.anisotropy = 8
  tex.generateMipmaps = true
  tex.minFilter = THREE.LinearMipmapLinearFilter

  const geo = new THREE.PlaneGeometry(width, (width * h) / w)
  const group = new THREE.Group()
  const opacity = { value: 1 }
  const materials: THREE.Material[] = []

  for (let i = layers; i >= 1; i--) {
    const m = new THREE.ShaderMaterial({
      vertexShader: vert,
      fragmentShader: layerFrag,
      uniforms: { uTex: { value: tex }, uDepth: { value: i / layers }, uOpacity: opacity },
    })
    materials.push(m)
    const mesh = new THREE.Mesh(geo, m)
    mesh.position.z = -(i / layers) * depth
    group.add(mesh)
  }

  const uniforms = {
    uTex: { value: tex },
    uTime: { value: 0 },
    uEnergy: { value: 0 },
    uFlow: { value: 1 },
    uLight: { value: new THREE.Vector2(0.8, 0.6) },
    uPointer: { value: new THREE.Vector2(0.5, 0.5) },
    uPointerOn: { value: 0 },
    uOpacity: opacity,
  }
  const face = new THREE.ShaderMaterial({ vertexShader: vert, fragmentShader: faceFrag, uniforms, transparent: true })
  materials.push(face)
  const faceMesh = new THREE.Mesh(geo, face)
  faceMesh.position.z = 0.001
  group.add(faceMesh)

  return {
    group,
    uniforms,
    aspect: w / h,
    dispose: () => {
      geo.dispose()
      tex.dispose()
      materials.forEach((m) => m.dispose())
    },
  }
}
