import { apiRequest } from './client'

export const TICKET_CATEGORIES = ['account', 'plans', 'billing', 'team', 'files', 'creations', 'voice', 'privacy', 'bug', 'other'] as const
export type TicketCategory = (typeof TICKET_CATEGORIES)[number]

/**
 * Categories the Core keeps that the app does not offer as a place to browse or to file under. Their articles still
 * answer a search, and the Core still accepts them; the app just does not lead with them. Voice is not a support
 * topic on the phone, creations live in the conversation that made them, and privacy has its own Settings screen.
 */
export const HIDDEN_SUPPORT_CATEGORIES: readonly string[] = ['voice', 'creations', 'privacy']

export const shownSupportCategory = (category: string) => !HIDDEN_SUPPORT_CATEGORIES.includes(category)

/** The category a new ticket starts on: the one the answer suggested when the app offers it, else "other". */
export function initialTicketCategory(suggested: string | null | undefined): TicketCategory {
  return suggested && shownSupportCategory(suggested) && (TICKET_CATEGORIES as readonly string[]).includes(suggested)
    ? (suggested as TicketCategory)
    : 'other'
}
export type TicketStatus = 'open' | 'in_review' | 'answered' | 'resolved'

export interface HelpArticle {
  id: string
  category: string
  title: string
  body: string
}

export interface AssistantAnswer {
  answer: string
  facts: string[]
  articles: HelpArticle[]
  suggest_ticket: boolean
  suggested_category: string
}

export interface TicketMessage {
  id: string
  author: 'user' | 'support'
  body: string
  created_at: string
}

export interface Ticket {
  id: string
  category: string
  subject: string
  status: TicketStatus
  created_at: string
  updated_at: string
  messages?: TicketMessage[]
}

export interface NewTicket {
  category: TicketCategory
  subject: string
  description: string
  platform: 'android' | 'ios' | 'web'
  app_version: string | null
  context?: { screen?: string; locale?: string; network?: string }
}

/** `category` narrows the results to one help-center category; the Core returns every category alongside them. */
export function searchArticles(
  query: string,
  lang: string,
  category?: string | null,
  signal?: AbortSignal,
): Promise<{ categories: string[]; articles: HelpArticle[] }> {
  const params = new URLSearchParams({ q: query, lang })
  if (category) params.set('category', category)
  return apiRequest(`/support/articles?${params.toString()}`, { signal })
}

export function askAssistant(message: string, lang: string): Promise<AssistantAnswer> {
  return apiRequest<AssistantAnswer>('/support/assistant', { method: 'POST', body: { message, lang } })
}

export function listTickets(signal?: AbortSignal): Promise<{ tickets: Ticket[] }> {
  return apiRequest<{ tickets: Ticket[] }>('/support/tickets', { signal })
}

export function openTicket(ticket: NewTicket): Promise<Ticket> {
  return apiRequest<Ticket>('/support/tickets', { method: 'POST', body: ticket })
}

export function getTicket(ticketId: string, signal?: AbortSignal): Promise<Ticket> {
  return apiRequest<Ticket>(`/support/tickets/${encodeURIComponent(ticketId)}`, { signal })
}

export function replyToTicket(ticketId: string, body: string): Promise<Ticket> {
  return apiRequest<Ticket>(`/support/tickets/${encodeURIComponent(ticketId)}/messages`, { method: 'POST', body: { body } })
}

export function resolveTicket(ticketId: string): Promise<Ticket> {
  return apiRequest<Ticket>(`/support/tickets/${encodeURIComponent(ticketId)}/resolve`, { method: 'POST' })
}
