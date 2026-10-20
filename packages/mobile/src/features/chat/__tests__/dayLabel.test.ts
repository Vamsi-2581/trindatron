import { dayLabel } from '../dayLabel'

const pt = { today: 'Hoje', yesterday: 'Ontem' }
const now = new Date(2026, 8, 16, 0, 10)

describe('day label', () => {
  it('says today and yesterday in words, by calendar day rather than by 24 hours', () => {
    expect(dayLabel(new Date(2026, 8, 16, 0, 1), now, 'pt-BR', pt)).toBe('Hoje')
    // Twenty minutes ago, but before midnight.
    expect(dayLabel(new Date(2026, 8, 15, 23, 50), now, 'pt-BR', pt)).toBe('Ontem')
  })

  it('writes older days as a date in the person language, with the year only when it is not this one', () => {
    expect(dayLabel(new Date(2026, 8, 2, 12), now, 'pt-BR', pt)).toBe('2 de setembro')
    expect(dayLabel(new Date(2025, 11, 31, 12), now, 'pt-BR', pt)).toBe('31 de dezembro de 2025')
    expect(dayLabel(new Date(2026, 8, 2, 12), now, 'en-US', { today: 'Today', yesterday: 'Yesterday' })).toBe('September 2')
  })

  it('shows nothing for a date it cannot read', () => {
    expect(dayLabel('not a date', now, 'pt-BR', pt)).toBeNull()
  })
})
