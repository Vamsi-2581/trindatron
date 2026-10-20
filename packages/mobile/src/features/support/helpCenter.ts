import type { Href } from 'expo-router'

import type { IconName } from '@/components/ui'
import { BILLING_HREF, PLANS_HREF } from '@/features/navigation/routes'

/** Icons for the categories the Core already uses; a category the app does not know yet still gets a card. */
const CATEGORY_ICONS: Record<string, IconName> = {
  account: 'person-circle-outline',
  plans: 'diamond-outline',
  billing: 'card-outline',
  team: 'people-outline',
  files: 'document-attach-outline',
  creations: 'images-outline',
  voice: 'mic-outline',
  privacy: 'shield-checkmark-outline',
  bug: 'construct-outline',
  other: 'help-circle-outline',
  conversations: 'chatbubbles-outline',
  agents: 'sparkles-outline',
  computer_use: 'desktop-outline',
  scheduled: 'alarm-outline',
}

/**
 * The page a help category is about, when the app has one. The "Planos" and "Cobrança" categories list help
 * articles; choosing a plan or managing a subscription happens on those pages, so the category points there too.
 */
export function productPageFor(category: string | null): { href: Href; labelKey: string; icon: IconName } | null {
  if (category === 'plans') return { href: PLANS_HREF, labelKey: 'support.openPlans', icon: 'diamond-outline' }
  if (category === 'billing') return { href: BILLING_HREF, labelKey: 'support.openBilling', icon: 'card-outline' }
  return null
}

export function categoryIcon(category: string): IconName {
  return CATEGORY_ICONS[category] ?? 'help-circle-outline'
}

/** The category's name in the person's language, or the Core's own id when the app has no words for it yet. */
export function categoryLabel(category: string, t: (key: string) => string): string {
  const key = `support.categories.${category}`
  const label = t(key)
  return label === key ? category.replace(/_/g, ' ') : label
}

/**
 * The assistant is offered only once a search has come back empty: the articles answer first, and the person
 * reaches the assistant with the words they already typed instead of a second form.
 */
export function shouldOfferAssistant(query: string, articleCount: number, loading: boolean): boolean {
  return query.trim().length > 0 && !loading && articleCount === 0
}
