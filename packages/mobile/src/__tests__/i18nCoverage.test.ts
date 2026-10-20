import fs from 'fs'
import path from 'path'

import en from '../i18n/locales/en.json'
import es from '../i18n/locales/es.json'
import pt from '../i18n/locales/pt.json'

const ROOT = path.resolve(__dirname, '../..')

function walk(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    return entry.isDirectory() ? walk(full) : [full]
  })
}

const SOURCE_FILES = [...walk(path.join(ROOT, 'app')), ...walk(path.join(ROOT, 'src'))].filter(
  (file) => /\.(ts|tsx)$/.test(file) && !/(__tests__|__integration__|[\\/]test[\\/]|\.d\.ts$)/.test(file),
)

function flatten(node: object, prefix = ''): string[] {
  return Object.entries(node).flatMap(([key, value]) =>
    typeof value === 'object' && value !== null ? flatten(value, `${prefix}${key}.`) : [`${prefix}${key}`],
  )
}

// Brand and product names are identical in every language.
const ALLOWED_LITERALS = new Set(['TRINDATRON'])

function findings(pattern: RegExp, files = SOURCE_FILES, group = 1): string[] {
  return files.flatMap((file) => {
    const source = fs.readFileSync(file, 'utf8')
    return [...source.matchAll(pattern)]
      .map((match) => match[group]?.trim() ?? '')
      .filter((text) => /[A-Za-zÀ-ÿ]{2,}/.test(text) && !ALLOWED_LITERALS.has(text))
      .map((text) => `${path.relative(ROOT, file)}: ${text}`)
  })
}

describe('i18n coverage', () => {
  it('every language has exactly the same keys', () => {
    const keys = flatten(pt).sort()
    expect(flatten(en).sort()).toEqual(keys)
    expect(flatten(es).sort()).toEqual(keys)
  })

  it('every key used in the app exists', () => {
    const known = new Set(flatten(pt))
    const missing = SOURCE_FILES.flatMap((file) =>
      [...fs.readFileSync(file, 'utf8').matchAll(/\bt\(\s*['"]([a-zA-Z0-9_.]+)['"]/g)]
        .map((match) => match[1]!)
        .filter((key) => !known.has(key))
        .map((key) => `${path.relative(ROOT, file)}: ${key}`),
    )
    expect(missing).toEqual([])
  })

  it('no JSX text is hardcoded', () => {
    const tsx = SOURCE_FILES.filter((file) => file.endsWith('.tsx'))
    expect(findings(/>([^<>{}]+)<\//g, tsx)).toEqual([])
  })

  it('no user-facing prop or message is a hardcoded string', () => {
    const props = /\b(?:label|title|placeholder|accessibilityLabel|accessibilityHint|subtitle|body|hint|confirmLabel|cta)=["']([^"']+)["']/g
    const calls = /\bset(?:Error|Notice|Message)\(\s*['"`]([^'"`]+)['"`]/g
    const objects = /\b(?:title|message|confirmLabel|subtitle|body):\s*['"`]([^'"`]+)['"`]/g
    expect([...findings(props), ...findings(calls), ...findings(objects)]).toEqual([])
  })
})
