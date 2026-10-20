/**
 * The sidebar must not tell a user that something they already have is
 * "coming soon".
 *
 * It did, for a while. The badge was the fallback branch of a check that
 * only knew about TRINITY CODE and TRINITY WORK, so every other workspace
 * — Projetos, Agentes, Agentes Persistentes — fell through to "EM BREVE"
 * even on a plan whose entitlement already enabled them, and even though
 * those screens were built and working. A product that does not know what
 * it ships is a worse signal than a missing feature.
 *
 * These read the component source rather than rendering it, the same way
 * marketing-claims.test.ts does, because what is being guarded is a
 * decision someone makes while editing a list — not a rendering behaviour.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect } from 'vitest'

const source = readFileSync(
  join(__dirname, '..', 'components', 'layout', 'sidebar.tsx'),
  'utf-8',
)

const NAV_BLOCK = source.slice(
  source.indexOf('const WORKSPACE_NAV'),
  source.indexOf(']', source.indexOf('const WORKSPACE_NAV')) + 1,
)

/** Every `{ href: '...', ..., built: true|false }` entry in the nav list. */
const entries = [...NAV_BLOCK.matchAll(/href:\s*'([^']+)'[^}]*built:\s*(true|false)/g)].map(
  ([, href, built]) => ({ href, built: built === 'true' }),
)

// No entry is a placeholder any more. /code and /work used to render a feature
// preview badged "Em breve" for capabilities the Core already shipped; each now
// says plainly what exists and where it starts (a project, a conversation), so
// the badge would be a lie. The `!built` branch stays in the component for a
// screen that genuinely is not built yet.
const PLACEHOLDER_ROUTES: string[] = []

describe('workspace nav', () => {
  it('declares a build state for every entry', () => {
    const hrefs = [...NAV_BLOCK.matchAll(/href:\s*'([^']+)'/g)].map(([, h]) => h)
    expect(entries.length).toBe(hrefs.length)
    expect(entries.length).toBeGreaterThan(0)
  })

  it('marks exactly the placeholder workspaces as not built', () => {
    const notBuilt = entries.filter((e) => !e.built).map((e) => e.href).sort()
    expect(notBuilt).toEqual([...PLACEHOLDER_ROUTES].sort())
  })

  it('names what the product does, not its internal components', () => {
    expect(NAV_BLOCK).not.toMatch(/TRINITY CODE|TRINITY WORK/)
  })

  it('never shows the badge for a workspace that is built', () => {
    // The badge is reachable only through the `!built` branch — not as the
    // fallback of the plan-gate check, which is how the bug happened.
    expect(source).toMatch(/\) : !built \? \(/)
    expect(source).not.toMatch(/locked \? \([\s\S]{0,200}\) : \(\s*<span[^>]*>\s*\{?\s*EM BREVE/)
  })

  it('shows a lock for a workspace the plan does not include', () => {
    expect(source).toMatch(/supports_trinity_code/)
    expect(source).toMatch(/supports_trinity_work/)
    // Agents are entitlement-gated too (FREE has supports_agent_mode=false
    // and max_agents=0), and used to show no affordance at all.
    expect(source).toMatch(/supports_agent_mode/)
  })
})
