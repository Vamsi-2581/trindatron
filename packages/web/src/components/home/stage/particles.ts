import * as THREE from 'three'

/**
 * One cloud of particles that re-forms itself for every chapter. Positions
 * are computed on the GPU from a per-particle seed, so a formation costs no
 * memory and morphing between two is a single uniform.
 */
export const Formation = {
  nebula: 0, // the hero: a tilted disc of light around the knot, deep dust behind
  waves: 1, // "you speak": rings leaving the centre like sound
  minds: 2, // several intelligences orbiting one core
  globe: 3, // the internet: a turning sphere with arcs travelling across it
  helix: 4, // memory: a double strand running through time
  page: 5, // files: a sheet of points being read by a scanning line
  voice: 6, // a living waveform
  gate: 7, // approval: a ring that holds what arrives until you say yes
  warp: 8, // what comes next: light rushing past
  dust: 9, // calm
} as const

const vert = /* glsl */ `
  attribute vec4 aSeed;
  uniform float uTime;
  uniform float uFrom;
  uniform float uTo;
  uniform float uMix;
  uniform float uSize;
  uniform float uPixelRatio;
  uniform vec3 uCenter;
  uniform float uPulse;
  uniform float uSpread;
  varying float vFade;
  varying float vTone;
  varying float vScan;

  const float TAU = 6.2831853;
  const float PI = 3.1415927;

  vec3 sphereDir(vec2 r) {
    float z = r.x * 2.0 - 1.0;
    float a = r.y * TAU;
    float q = sqrt(max(0.0, 1.0 - z * z));
    return vec3(q * cos(a), q * sin(a), z);
  }
  vec3 rotY(vec3 p, float a) { float c = cos(a), s = sin(a); return vec3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z); }
  vec3 rotX(vec3 p, float a) { float c = cos(a), s = sin(a); return vec3(p.x, c * p.y - s * p.z, s * p.y + c * p.z); }

  // Returns position (xyz) and a fade (w) for formation id.
  vec4 formation(float id, vec4 s, float t) {
    if (id < 0.5) { // nebula
      if (s.w < 0.72) {
        float r = 1.9 + pow(s.x, 1.7) * 4.2;
        float a = s.y * TAU + t * 0.06 * (3.0 / r);
        vec3 p = vec3(cos(a) * r, (s.z - 0.5) * 0.28 * (1.0 + r * 0.3), sin(a) * r);
        return vec4(rotX(p, 0.42), 1.0 - smoothstep(3.6, 6.1, r) * 0.8);
      }
      return vec4((s.y - 0.5) * 34.0, (s.z - 0.5) * 20.0, -5.0 - s.x * 20.0, 0.28);
    }
    if (id < 1.5) { // waves
      float k = floor(s.x * 7.0);
      float ph = fract(k / 7.0 + t * 0.11);
      float r = 1.5 + ph * 5.2;
      float a = s.y * TAU;
      return vec4(cos(a) * r, sin(a) * r * 0.92, (s.z - 0.5) * 0.25, (1.0 - ph) * smoothstep(0.0, 0.06, ph));
    }
    if (id < 2.5) { // minds
      float c = floor(s.x * 5.0);
      float a = c / 5.0 * TAU + t * 0.12;
      vec3 centre = vec3(cos(a) * 3.1, sin(t * 0.35 + c * 1.7) * 0.5, sin(a) * 1.7);
      if (s.w < 0.14) {
        float f = fract(s.y * 3.0 + t * 0.45 + c * 0.2);
        return vec4(mix(vec3(0.0), centre, f), sin(f * PI));
      }
      return vec4(centre + sphereDir(s.yz) * 0.62 * pow(s.w, 0.33), 1.0);
    }
    if (id < 3.5) { // globe
      if (s.w < 0.78) {
        vec3 d = sphereDir(s.xy);
        return vec4(rotY(d * 2.7, t * 0.14), 0.55 + 0.45 * smoothstep(-0.2, 1.0, rotY(d, t * 0.14).z));
      }
      vec3 p0 = sphereDir(s.xy);
      vec3 p1 = sphereDir(s.zw);
      float f = fract(s.x * 7.0 + t * 0.22);
      vec3 p = normalize(mix(p0, p1, f)) * (2.7 + sin(f * PI) * 1.0);
      return vec4(rotY(p, t * 0.14), sin(f * PI));
    }
    if (id < 4.5) { // helix
      float x = (s.x - 0.5) * 15.0;
      float strand = step(0.5, s.y);
      float a = x * 0.85 + t * 0.55 + strand * PI;
      vec3 p = vec3(x, cos(a) * 1.15, sin(a) * 1.15) + (vec3(s.z, s.w, s.y) - 0.5) * 0.12;
      return vec4(rotY(p, -0.35), (1.0 - smoothstep(4.0, 7.5, abs(x))) * (0.6 + 0.4 * sin(a)));
    }
    if (id < 5.5) { // page
      if (s.w < 0.82) {
        float i = floor(s.x * 2016.0);
        float col = mod(i, 42.0);
        float row = floor(i / 42.0);
        vec3 p = vec3((col / 41.0 - 0.5) * 3.1, (row / 47.0 - 0.5) * 4.1, 0.0);
        return vec4(rotX(rotY(p, -0.42), -0.12), 0.8);
      }
      return vec4((s.y - 0.5) * 22.0, (s.z - 0.5) * 12.0, -4.0 - s.x * 12.0, 0.35);
    }
    if (id < 6.5) { // voice
      float x = (s.x - 0.5) * 13.0;
      float env = exp(-x * x * 0.028);
      float amp = 0.55 + 0.45 * sin(t * 1.6) * sin(t * 0.7 + 1.0);
      float y = (sin(x * 1.35 + t * 3.0) * 0.8 + sin(x * 3.2 - t * 4.2) * 0.3) * env * amp;
      return vec4(x, y + (s.y - 0.5) * 0.14 * (0.4 + env), (s.z - 0.5) * 0.5, 0.35 + 0.65 * env);
    }
    if (id < 7.5) { // gate
      if (s.w < 0.86) {
        float r = 2.55 + (s.y - 0.5) * 0.18;
        float a = s.x * TAU + t * 0.16;
        return vec4(cos(a) * r, sin(a) * r, (s.z - 0.5) * 0.2, 1.0);
      }
      // What arrives waits at the ring, then dissolves: nothing passes without a yes.
      float f = fract(s.x + t * 0.07);
      float x = mix(7.0, 2.9, smoothstep(0.0, 0.7, f));
      float fade = smoothstep(0.0, 0.1, f) * (1.0 - smoothstep(0.7, 0.9, f)) * 0.6;
      return vec4(x + s.y * 0.3, (s.z - 0.5) * 1.6, (s.y - 0.5) * 0.4, fade);
    }
    if (id < 8.5) { // warp
      float a = s.y * TAU;
      float rad = 0.5 + s.x * 8.0;
      float z = mod(s.z * 60.0 + t * 20.0, 60.0) - 52.0;
      return vec4(cos(a) * rad, sin(a) * rad, z, smoothstep(-52.0, -40.0, z));
    }
    // dust
    return vec4((s.x - 0.5) * 26.0 + sin(t * 0.05 + s.w * 6.0) * 0.4, (s.y - 0.5) * 15.0 + t * 0.02 * (s.z - 0.5), -2.0 - s.z * 12.0, 0.5);
  }

  void main() {
    vec4 a = formation(uFrom, aSeed, uTime);
    vec4 b = formation(uTo, aSeed, uTime);
    float m = smoothstep(0.0, 1.0, clamp(uMix * 1.15 - aSeed.w * 0.15, 0.0, 1.0));
    vec3 p = mix(a.xyz, b.xyz, m) * uSpread;
    vFade = mix(a.w, b.w, m);
    vTone = aSeed.w;

    vec3 world = p + uCenter;
    // A tap sends a shockwave out from the knot.
    float age = uTime - uPulse;
    if (age > 0.0 && age < 2.2) {
      vec3 d = world - uCenter;
      float dist = length(d) + 0.0001;
      float wave = exp(-pow(dist - age * 5.5, 2.0) * 1.6) * (1.0 - age / 2.2);
      world += d / dist * wave * 0.7;
      vFade += wave * 0.8;
    }
    vScan = exp(-pow((p.y - sin(uTime * 0.8) * 2.1) * 3.5, 2.0)) * step(4.5, uTo) * step(uTo, 5.5);

    vec4 mv = modelViewMatrix * vec4(world, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = uSize * (0.55 + aSeed.w * 1.5) * uPixelRatio * (8.0 / max(0.5, -mv.z));
  }
`

const frag = /* glsl */ `
  uniform vec3 uColorA;
  uniform vec3 uColorB;
  uniform float uOpacity;
  varying float vFade;
  varying float vTone;
  varying float vScan;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    float d = dot(c, c);
    if (d > 0.25) discard;
    float soft = smoothstep(0.25, 0.0, d);
    vec3 col = mix(uColorA, uColorB, smoothstep(0.35, 1.0, vTone)) * (1.0 + vScan * 2.5);
    gl_FragColor = vec4(col, soft * clamp(vFade, 0.0, 1.4) * uOpacity);
  }
`

export interface Particles {
  points: THREE.Points
  uniforms: {
    uTime: { value: number }
    uFrom: { value: number }
    uTo: { value: number }
    uMix: { value: number }
    uSize: { value: number }
    uPixelRatio: { value: number }
    uCenter: { value: THREE.Vector3 }
    uPulse: { value: number }
    uSpread: { value: number }
    uColorA: { value: THREE.Color }
    uColorB: { value: THREE.Color }
    uOpacity: { value: number }
  }
  dispose: () => void
}

export function createParticles(count: number, pixelRatio: number): Particles {
  const geo = new THREE.BufferGeometry()
  const seeds = new Float32Array(count * 4)
  // Deterministic seeds: the same visitor sees the same sky on every visit.
  let s = 1234567
  const rnd = () => ((s = (s * 16807) % 2147483647) / 2147483647)
  for (let i = 0; i < count * 4; i++) seeds[i] = rnd()
  geo.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 4))
  // Positions are computed in the shader; this attribute only sizes the draw.
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(count * 3), 3))
  geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 100)

  const uniforms = {
    uTime: { value: 0 },
    uFrom: { value: Formation.nebula as number },
    uTo: { value: Formation.nebula as number },
    uMix: { value: 1 },
    uSize: { value: 2.2 },
    uPixelRatio: { value: pixelRatio },
    uCenter: { value: new THREE.Vector3() },
    uPulse: { value: -10 },
    uSpread: { value: 1 },
    uColorA: { value: new THREE.Color('#2fd8e8') },
    uColorB: { value: new THREE.Color('#e8fbff') },
    uOpacity: { value: 0.9 },
  }
  const mat = new THREE.ShaderMaterial({
    vertexShader: vert,
    fragmentShader: frag,
    uniforms,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  })
  const points = new THREE.Points(geo, mat)
  points.frustumCulled = false
  return { points, uniforms, dispose: () => { geo.dispose(); mat.dispose() } }
}
