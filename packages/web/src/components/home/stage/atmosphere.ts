import * as THREE from 'three'

/**
 * The room the knot floats in: a full-screen gradient with slow fog, a shaft
 * of light and a vignette, tinted per chapter. Drawn first, behind everything,
 * so the page is never a flat black.
 */
const vert = /* glsl */ `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = vec4(position.xy, 0.9999, 1.0); }
`

const frag = /* glsl */ `
  uniform float uTime;
  uniform vec2 uRes;
  uniform vec3 uTop;
  uniform vec3 uBottom;
  uniform vec3 uTint;
  uniform float uFog;
  uniform float uShaftX;
  varying vec2 vUv;

  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
  }
  float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 4; i++) { v += a * noise(p); p *= 2.03; a *= 0.5; }
    return v;
  }

  void main() {
    vec2 uv = vUv;
    float aspect = uRes.x / max(1.0, uRes.y);
    vec3 col = mix(uBottom, uTop, smoothstep(-0.1, 1.15, uv.y));
    vec2 q = vec2(uv.x * aspect, uv.y);
    float fog = fbm(q * 1.6 + vec2(uTime * 0.012, -uTime * 0.006));
    fog = smoothstep(0.35, 0.95, fog);
    col += uTint * fog * uFog;
    float shaft = exp(-pow((uv.x - uShaftX - (1.0 - uv.y) * 0.12) * 3.2, 2.0)) * smoothstep(0.0, 1.0, uv.y);
    col += uTint * shaft * 0.07;
    float v = length((uv - 0.5) * vec2(aspect * 0.8, 1.0));
    col *= 1.0 - 0.55 * smoothstep(0.35, 1.05, v);
    col += (hash(uv * uRes + fract(uTime)) - 0.5) * 0.006;
    gl_FragColor = vec4(max(col, vec3(0.0)), 1.0);
  }
`

export interface Atmosphere {
  mesh: THREE.Mesh
  uniforms: {
    uTime: { value: number }
    uRes: { value: THREE.Vector2 }
    uTop: { value: THREE.Color }
    uBottom: { value: THREE.Color }
    uTint: { value: THREE.Color }
    uFog: { value: number }
    uShaftX: { value: number }
  }
  dispose: () => void
}

export function createAtmosphere(): Atmosphere {
  const geo = new THREE.PlaneGeometry(2, 2)
  const uniforms = {
    uTime: { value: 0 },
    uRes: { value: new THREE.Vector2(1, 1) },
    uTop: { value: new THREE.Color('#0b1116') },
    uBottom: { value: new THREE.Color('#030405') },
    uTint: { value: new THREE.Color('#1b8fa0') },
    uFog: { value: 0.12 },
    uShaftX: { value: 0.66 },
  }
  const mat = new THREE.ShaderMaterial({ vertexShader: vert, fragmentShader: frag, uniforms, depthWrite: false, depthTest: false })
  const mesh = new THREE.Mesh(geo, mat)
  mesh.frustumCulled = false
  mesh.renderOrder = -10
  return { mesh, uniforms, dispose: () => { geo.dispose(); mat.dispose() } }
}
