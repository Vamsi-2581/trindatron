import type { BadgeTone } from '@/components/ui/Badge'
import type { TicketStatus } from '@/services/api/support'

const TONES: Record<TicketStatus, BadgeTone> = {
  open: 'warning',
  in_review: 'accent',
  answered: 'success',
  resolved: 'neutral',
}

/** Badge tone for a support ticket status; unknown statuses from newer Cores stay neutral. */
export function statusTone(status: TicketStatus): BadgeTone {
  return TONES[status] ?? 'neutral'
}
