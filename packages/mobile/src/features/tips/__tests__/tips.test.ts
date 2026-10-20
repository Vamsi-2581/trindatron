import { baselineCapabilities, type Capabilities } from '@/features/capabilities/capabilities'
import { translate, type Locale } from '@/i18n'

import { eligibleTips, pickTip, TIPS, type Tip } from '../tips'

function withCapabilities(on: (keyof Capabilities)[]): Capabilities {
  const capabilities = baselineCapabilities()
  for (const id of on) capabilities[id] = { available: true, status: 'available', dailyQuota: null, limits: null }
  return capabilities
}

/** What the Core sends for a capability the plan does not include. */
function planGated(ids: (keyof Capabilities)[]): Capabilities {
  const capabilities = baselineCapabilities()
  for (const id of ids) capabilities[id] = { available: false, status: 'plan', dailyQuota: null, limits: null }
  return capabilities
}

const ids = (list: { id: string }[]) => list.map((tip) => tip.id)

describe('product tips', () => {
  it('never advertises media, image analysis or document reading the Core does not offer', () => {
    const tips = ids(eligibleTips({ capabilities: baselineCapabilities(), plan: 'pro' }))
    for (const id of ['imageCreate', 'videoCreate', 'imageAnalyze', 'documentSummary']) expect(tips).not.toContain(id)
  })

  it('turns those tips on as soon as the Core reports the capability', () => {
    const tips = ids(
      eligibleTips({ capabilities: withCapabilities(['image_generation', 'video_generation', 'chat_attachments', 'image_understanding']), plan: 'pro' }),
    )
    expect(tips).toEqual(expect.arrayContaining(['imageCreate', 'videoCreate', 'imageAnalyze', 'documentSummary']))
  })

  it('reading documents does not imply understanding images', () => {
    const tips = ids(eligibleTips({ capabilities: withCapabilities(['chat_attachments']), plan: 'pro' }))
    expect(tips).toContain('documentSummary')
    expect(tips).not.toContain('imageAnalyze')
  })

  it('suggests upgrading only for what the current plan lacks', () => {
    const gated = eligibleTips({ capabilities: planGated(['agent_mode', 'presentations']), plan: 'free' })
    expect(gated.filter((tip) => tip.upgrade).map((tip) => tip.action?.href)).toEqual(['/plans', '/plans'])

    const paid = ids(eligibleTips({ capabilities: withCapabilities(['agent_mode', 'persistent_agents', 'presentations']), plan: 'pro' }))
    expect(paid).not.toContain('agentUpgrade')
    expect(paid).not.toContain('presentationsUpgrade')
    expect(paid).toEqual(expect.arrayContaining(['agentTasks', 'persistentAgents', 'presentations']))
  })

  it('never sells an upgrade for something that is merely down', () => {
    // The baseline says "unavailable", which is what an outage looks like. Offering to sell the
    // capability then is both wrong and insulting; only a plan gate justifies the tip.
    const tips = ids(eligibleTips({ capabilities: baselineCapabilities(), plan: 'free' }))
    expect(tips).not.toContain('agentUpgrade')
    expect(tips).not.toContain('presentationsUpgrade')
  })

  it('rotates: the same tip never shows twice in a row, and the cycle wraps around', () => {
    const context = { capabilities: planGated(['agent_mode', 'presentations']), plan: 'free' }
    const eligible = eligibleTips(context)
    let last: string | null = null
    const seen: string[] = []
    for (let visit = 0; visit < eligible.length + 1; visit++) {
      const tip: Tip = pickTip(context, last)!
      expect(tip.id).not.toBe(last)
      seen.push(tip.id)
      last = tip.id
    }
    expect(new Set(seen).size).toBe(eligible.length)
    expect(seen[eligible.length]).toBe(seen[0])
  })

  it('is short and translated in every language', () => {
    for (const locale of ['pt', 'en', 'es'] as Locale[]) {
      for (const tip of TIPS) {
        const text = translate(locale, tip.textKey)
        expect(text).not.toBe(tip.textKey)
        expect(text.length).toBeLessThanOrEqual(120)
        expect(translate(locale, `tips.label.${tip.kind}`)).not.toBe(`tips.label.${tip.kind}`)
        if (tip.action) expect(translate(locale, tip.action.labelKey)).not.toBe(tip.action.labelKey)
      }
    }
  })
})
