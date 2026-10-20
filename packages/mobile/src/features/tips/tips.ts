import type { Href } from 'expo-router'

import { statusOf, type Capabilities, type CapabilityId } from '@/features/capabilities/capabilities'
import { PLANS_HREF } from '@/features/navigation/routes'

export type TipKind = 'didYouKnow' | 'tip' | 'try' | 'create'

export interface TipContext {
  capabilities: Capabilities
  plan: string | null
}

export interface Tip {
  id: string
  kind: TipKind
  /** i18n key of the sentence. */
  textKey: string
  action?: { labelKey: string; href: Href }
  /** A tip about something the current plan does not include. */
  upgrade?: boolean
  eligible: (context: TipContext) => boolean
}

/**
 * Whether the Core says this capability is missing *because of the plan* — not
 * because an engine is down or because it was never built. This used to be
 * answered by reading the plan name, which is a product rule the app does not
 * own: an upgrade tip then appeared whenever anything was off, including during
 * an outage, when suggesting a purchase is exactly the wrong thing to do.
 */
const notInPlan = (context: TipContext, id: CapabilityId) => statusOf(context.capabilities[id]) === 'plan'

/**
 * Every tip is tied to something the Core really offers this user today (or,
 * for upgrade tips, offers on a paid plan). A tip about image analysis,
 * document summaries or media creation only becomes eligible when the Core
 * reports that capability — never because the sentence exists.
 */
export const TIPS: readonly Tip[] = [
  { id: 'outcome', kind: 'tip', textKey: 'tips.outcome', eligible: () => true },
  { id: 'voice', kind: 'tip', textKey: 'tips.voice', eligible: (c) => c.capabilities.voice_input.available },
  {
    id: 'projectInstructions',
    kind: 'try',
    textKey: 'tips.projectInstructions',
    action: { labelKey: 'tips.openProjects', href: '/projects' },
    eligible: (c) => c.capabilities.project_files.available,
  },
  { id: 'agentTasks', kind: 'didYouKnow', textKey: 'tips.agentTasks', eligible: (c) => c.capabilities.agent_mode.available },
  {
    id: 'agentUpgrade',
    kind: 'didYouKnow',
    textKey: 'tips.agentUpgrade',
    upgrade: true,
    action: { labelKey: 'tips.seePlans', href: PLANS_HREF },
    eligible: (c) => notInPlan(c, 'agent_mode'),
  },
  {
    id: 'persistentAgents',
    kind: 'didYouKnow',
    textKey: 'tips.persistentAgents',
    action: { labelKey: 'tips.openAgents', href: '/agents' },
    eligible: (c) => c.capabilities.persistent_agents.available,
  },
  { id: 'presentations', kind: 'create', textKey: 'tips.presentations', eligible: (c) => c.capabilities.presentations.available },
  {
    id: 'presentationsUpgrade',
    kind: 'create',
    textKey: 'tips.presentationsUpgrade',
    upgrade: true,
    action: { labelKey: 'tips.seePlans', href: PLANS_HREF },
    eligible: (c) => notInPlan(c, 'presentations'),
  },
  { id: 'imageCreate', kind: 'create', textKey: 'tips.imageCreate', eligible: (c) => c.capabilities.image_generation.available },
  { id: 'videoCreate', kind: 'create', textKey: 'tips.videoCreate', eligible: (c) => c.capabilities.video_generation.available },
  { id: 'imageAnalyze', kind: 'tip', textKey: 'tips.imageAnalyze', eligible: (c) => c.capabilities.image_understanding.available },
  { id: 'documentSummary', kind: 'try', textKey: 'tips.documentSummary', eligible: (c) => c.capabilities.chat_attachments.available },
]

export function eligibleTips(context: TipContext): Tip[] {
  return TIPS.filter((tip) => tip.eligible(context))
}

/** Next eligible tip after the one shown last time, so the same tip never shows twice in a row. */
export function pickTip(context: TipContext, lastId: string | null): Tip | null {
  const tips = eligibleTips(context)
  if (tips.length === 0) return null
  const index = tips.findIndex((tip) => tip.id === lastId)
  return tips[(index + 1) % tips.length] ?? null
}
