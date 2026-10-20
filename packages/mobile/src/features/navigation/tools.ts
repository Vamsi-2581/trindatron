import type { Href } from 'expo-router'

import type { IconName } from '@/components/ui'
import { MEDIA_CAPABILITIES, statusOf, type Capabilities, type CapabilityId } from '@/features/capabilities/capabilities'

import { BILLING_HREF, COMPUTER_USE_HREF, PLANS_HREF, SCHEDULED_HREF, USAGE_HREF } from './routes'

export interface Tool {
  key: string
  icon: IconName
  labelKey: string
  href: Href
}

/**
 * The places beside the conversation, and the capabilities that make each one exist. A tool is shown when any of
 * its capabilities is part of the product for this person — available, or in the product but not in their plan or
 * down right now, which its own screen explains. Only something the Core says is not integrated is left out: the
 * menu never offers a place that has nothing behind it. `requires: []` is always there (Agendados is a Core feature
 * of every account; its screen shows the plan's answer).
 */
const TOOLS: (Tool & { requires: readonly CapabilityId[] })[] = [
  { key: 'creations', icon: 'images-outline', labelKey: 'creations.title', href: '/creations', requires: MEDIA_CAPABILITIES },
  { key: 'scheduled', icon: 'alarm-outline', labelKey: 'nav.scheduled', href: SCHEDULED_HREF, requires: [] },
  { key: 'computerUse', icon: 'desktop-outline', labelKey: 'nav.computerUse', href: COMPUTER_USE_HREF, requires: ['browser_control', 'computer_control'] },
  { key: 'agents', icon: 'sparkles-outline', labelKey: 'nav.agents', href: '/agents', requires: ['agent_mode', 'persistent_agents'] },
  { key: 'projects', icon: 'folder-outline', labelKey: 'nav.projects', href: '/projects', requires: ['project_files'] },
  { key: 'files', icon: 'document-text-outline', labelKey: 'nav.files', href: '/files', requires: ['project_files'] },
]

/** The account's own pages: every account has them, whatever its capabilities. Each one is its own screen. */
export const ACCOUNT_PLACES: Tool[] = [
  { key: 'usage', icon: 'speedometer-outline', labelKey: 'nav.usage', href: USAGE_HREF },
  { key: 'plans', icon: 'diamond-outline', labelKey: 'nav.plans', href: PLANS_HREF },
  { key: 'billing', icon: 'card-outline', labelKey: 'nav.billing', href: BILLING_HREF },
]

export function visibleTools(capabilities: Capabilities): Tool[] {
  return TOOLS.filter(
    ({ requires }) => requires.length === 0 || requires.some((id) => statusOf(capabilities[id]) !== 'not_integrated'),
  ).map(({ requires: _requires, ...tool }) => tool)
}
