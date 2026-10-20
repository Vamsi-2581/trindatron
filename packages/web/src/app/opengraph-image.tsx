import { ImageResponse } from 'next/og'

// Social card, rendered once at build time. Same triad geometry and the same
// design tokens as the site itself, so a shared link looks like the product
// rather than a stock template.
export const alt = 'TRINDATRON — você dá o objetivo, ele decide como executar'
export const size = { width: 1200, height: 630 }
export const contentType = 'image/png'

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: '100%',
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
          background: '#07080a',
          padding: '72px 80px',
          position: 'relative',
        }}
      >
        {/* Accent bloom, top-right — the same glow the hero uses. */}
        <div
          style={{
            position: 'absolute',
            top: -220,
            right: -160,
            width: 700,
            height: 700,
            borderRadius: 700,
            background: 'radial-gradient(circle, rgba(0,194,212,0.34) 0%, rgba(0,194,212,0) 68%)',
            display: 'flex',
          }}
        />

        {/* Lockup */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 22 }}>
          <div style={{ display: 'flex', position: 'relative', width: 48, height: 42 }}>
            <div style={dot(24, 6, 10.5, '#00c2d4', 1)} />
            <div style={dot(6.75, 36, 10.5, '#00c2d4', 0.6)} />
            <div style={dot(41.25, 36, 10.5, '#00c2d4', 0.35)} />
          </div>
          <div
            style={{
              fontSize: 30,
              fontWeight: 600,
              letterSpacing: 4.5,
              color: '#eef2f6',
              display: 'flex',
            }}
          >
            TRINDATRON
          </div>
        </div>

        {/* Message */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 26 }}>
          <div
            style={{
              fontSize: 66,
              fontWeight: 600,
              lineHeight: 1.08,
              letterSpacing: -2,
              color: '#eef2f6',
              display: 'flex',
              maxWidth: 920,
            }}
          >
            Você dá o objetivo. Ele decide como executar.
          </div>
          <div
            style={{
              fontSize: 27,
              lineHeight: 1.45,
              color: '#8585a0',
              display: 'flex',
              maxWidth: 800,
            }}
          >
            Entende o pedido, aciona os agentes, usa as ferramentas, executa e
            verifica o resultado.
          </div>
        </div>

        {/* Footer rule */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
          <div style={{ display: 'flex', width: 56, height: 3, background: '#00c2d4' }} />
          <div style={{ fontSize: 23, color: '#4a4962', display: 'flex' }}>trindatron.com</div>
        </div>
      </div>
    ),
    size,
  )
}

function dot(cx: number, cy: number, r: number, color: string, opacity: number) {
  return {
    position: 'absolute' as const,
    left: cx - r,
    top: cy - r,
    width: r * 2,
    height: r * 2,
    borderRadius: r * 2,
    background: color,
    opacity,
    display: 'flex',
  }
}
