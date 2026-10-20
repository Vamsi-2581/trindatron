import type { IconName } from '@/components/ui'
import type { Capabilities } from '@/features/capabilities/capabilities'

export type Suggestion =
  | { key: string; icon: IconName; labelKey: string; kind: 'prompt'; promptKey: string }
  | { key: string; icon: IconName; labelKey: string; kind: 'create'; create: 'image' | 'video' }

/** Conversation is always there; every other starter exists only while the Core says its capability is available. */
const ALWAYS: Suggestion[] = [
  { key: 'write', icon: 'create-outline', labelKey: 'home.suggestion.write', kind: 'prompt', promptKey: 'home.prompt.write' },
  { key: 'explain', icon: 'bulb-outline', labelKey: 'home.suggestion.explain', kind: 'prompt', promptKey: 'home.prompt.explain' },
]

const WHEN_AVAILABLE: { requires: keyof Capabilities; suggestion: Suggestion }[] = [
  { requires: 'image_generation', suggestion: { key: 'image', icon: 'color-palette-outline', labelKey: 'home.suggestion.image', kind: 'create', create: 'image' } },
  { requires: 'video_generation', suggestion: { key: 'video', icon: 'film-outline', labelKey: 'home.suggestion.video', kind: 'create', create: 'video' } },
  // "Aja como agente" is the wording the Core's classifier routes to agent mode.
  { requires: 'agent_mode', suggestion: { key: 'agent', icon: 'sparkles-outline', labelKey: 'home.suggestion.agent', kind: 'prompt', promptKey: 'home.prompt.agent' } },
  { requires: 'chat_attachments', suggestion: { key: 'document', icon: 'document-text-outline', labelKey: 'home.suggestion.document', kind: 'prompt', promptKey: 'home.prompt.document' } },
]

const MAX_SUGGESTIONS = 4

/**
 * Starters for a new conversation, derived only from the capability matrix — never from the model, never
 * offering something this account cannot do right now. What the account can do goes first.
 */
export function startSuggestions(capabilities: Capabilities): Suggestion[] {
  const specific = WHEN_AVAILABLE.filter(({ requires }) => capabilities[requires].available).map(({ suggestion }) => suggestion)
  return [...specific, ...ALWAYS].slice(0, MAX_SUGGESTIONS)
}
