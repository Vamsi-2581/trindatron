import fs from 'fs'
import path from 'path'

import { BRAND_ASSETS, pickSymbolAsset, SYMBOL_SIZE, WORDMARK_SIZE } from '@/components/ui/Brand'

import app from '../../app.json'

const ROOT = path.resolve(__dirname, '../..')

function pngSize(relativePath: string): { width: number; height: number } {
  const buffer = fs.readFileSync(path.join(ROOT, relativePath))
  expect(buffer.subarray(1, 4).toString('ascii')).toBe('PNG')
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) }
}

type PluginEntry = string | [string, Record<string, unknown>]

describe('brand assets', () => {
  it('keeps the master artwork untouched', () => {
    expect(pngSize('assets/brand/trindatron-symbol.png')).toEqual({ width: 792, height: 545 })
    expect(fs.existsSync(path.join(ROOT, '../web/public/brand/master/source/logo-oficial-lockup.jpg'))).toBe(true)
  })

  it('picks an official file with enough pixels for the size and screen density', () => {
    expect(pickSymbolAsset(14, 3).width).toBe(48)
    expect(pickSymbolAsset(28, 3).width).toBe(96)
    expect(pickSymbolAsset(40, 3).width).toBe(128)
    expect(pickSymbolAsset(50, 3).width).toBe(160)
    expect(pickSymbolAsset(96, 3).width).toBe(320)
    expect(pickSymbolAsset(200, 3).width).toBe(SYMBOL_SIZE.width)
    // Never upscales: past the matte's resolution it stays on the largest official file.
    expect(pickSymbolAsset(400, 3).width).toBe(SYMBOL_SIZE.width)
  })

  it('no UI size makes the screen shrink a file by more than ~1.35x (the cause of jagged thin lines)', () => {
    for (let heightPt = 12; heightPt <= 120; heightPt++) {
      for (const density of [2, 2.625, 2.75, 3, 3.5]) {
        const needed = heightPt * (SYMBOL_SIZE.width / SYMBOL_SIZE.height) * density
        const picked = pickSymbolAsset(heightPt, density)
        if (picked.width < SYMBOL_SIZE.width && needed >= 48) expect(picked.width / needed).toBeLessThanOrEqual(1.35)
      }
    }
  })

  it('every derived file has the resolution its code expects, and no size is an upscale', () => {
    expect(pngSize('assets/brand/trindatron-symbol-hd.png')).toEqual(SYMBOL_SIZE)
    expect(pngSize('assets/brand/trindatron-symbol-matte.png')).toEqual(SYMBOL_SIZE)
    for (const width of [512, 384, 320, 256, 192, 160, 128, 96, 80, 64, 48]) {
      const size = pngSize(`assets/brand/trindatron-symbol-${width}.png`)
      expect(size.width).toBe(width)
      expect(size.width).toBeLessThanOrEqual(SYMBOL_SIZE.width)
      expect(Math.abs(size.width / size.height - SYMBOL_SIZE.width / SYMBOL_SIZE.height)).toBeLessThan(0.02)
    }
    expect(SYMBOL_SIZE.width).toBeLessThanOrEqual(792)
    expect(pngSize('assets/brand/trindatron-wordmark.png')).toEqual(WORDMARK_SIZE)
  })

  it('the opening animation layers line up with the symbol, so at rest they recompose it exactly', () => {
    expect(pngSize('assets/brand/trindatron-intro-upper.png')).toEqual(SYMBOL_SIZE)
    expect(pngSize('assets/brand/trindatron-intro-lower.png')).toEqual(SYMBOL_SIZE)
    expect(Object.keys(BRAND_ASSETS).sort()).toEqual(['glow', 'introLower', 'introUpper', 'symbolHd', 'wordmark'])
  })

  it('the store icon, adaptive icon and splash use the re-matted official artwork', () => {
    expect(app.expo.icon).toBe('./assets/brand/trindatron-icon-1024.png')
    expect(pngSize(app.expo.icon)).toEqual({ width: 1024, height: 1024 })

    const foreground = app.expo.android.adaptiveIcon.foregroundImage
    expect(pngSize(foreground)).toEqual({ width: 1024, height: 1024 })
    expect(app.expo.android.adaptiveIcon.backgroundColor).toBe('#07080a')

    const splash = (app.expo.plugins as PluginEntry[]).find((plugin) => Array.isArray(plugin) && plugin[0] === 'expo-splash-screen') as [
      string,
      { image: string; backgroundColor: string },
    ]
    expect(splash[1].image).toBe('./assets/brand/trindatron-symbol-hd.png')
    expect(splash[1].backgroundColor).toBe('#07080a')
    expect(pngSize(app.expo.web.favicon)).toEqual({ width: 32, height: 32 })
  })

  it('keeps the official cyan and never the retired purple', () => {
    const tokens = fs.readFileSync(path.join(ROOT, 'src/theme/tokens.ts'), 'utf8')
    expect(tokens).toContain('#00c2d4')
    expect(tokens.toLowerCase()).not.toContain("'#7c3aed'")
  })
})
