import { apiRequest } from '../client'
import { askAssistant, getTicket, listTickets, openTicket, replyToTicket, resolveTicket, searchArticles } from '../support'

jest.mock('../client', () => ({
  apiRequest: jest.fn(),
  authHeaders: jest.fn(() => ({ Authorization: 'Bearer t' })),
  buildUrl: jest.fn((path: string) => `https://core.example.com${path}`),
}))

const request = apiRequest as jest.Mock

describe('support API', () => {
  beforeEach(() => request.mockReset().mockResolvedValue({}))

  it('searches the help center in the reader’s own language', async () => {
    await searchArticles('cobrança', 'pt')
    expect(request).toHaveBeenCalledWith('/support/articles?q=cobran%C3%A7a&lang=pt', { signal: undefined })
  })

  it('narrows to one category only when one was chosen', async () => {
    // The Core returns the categories it really has; "all" is the absence of the filter, not a category
    // of its own, so asking for everything must not send `category=all` — the Core would match nothing.
    await searchArticles('', 'pt', null)
    expect(request).toHaveBeenLastCalledWith('/support/articles?q=&lang=pt', { signal: undefined })

    await searchArticles('', 'en', 'billing')
    expect(request).toHaveBeenLastCalledWith('/support/articles?q=&lang=en&category=billing', { signal: undefined })
  })

  it('asks the help assistant, which answers from the articles and this account', async () => {
    await askAssistant('como cancelo?', 'pt')
    expect(request).toHaveBeenLastCalledWith('/support/assistant', {
      method: 'POST',
      body: { message: 'como cancelo?', lang: 'pt' },
    })
  })

  it('opens a ticket with the facts that help an operator answer it', async () => {
    await openTicket({
      category: 'billing',
      subject: 'Cobrança duplicada',
      description: 'Fui cobrado duas vezes.',
      platform: 'android',
      app_version: '0.1.0',
      context: { screen: 'support', locale: 'pt' },
    })
    expect(request).toHaveBeenLastCalledWith('/support/tickets', {
      method: 'POST',
      body: {
        category: 'billing',
        subject: 'Cobrança duplicada',
        description: 'Fui cobrado duas vezes.',
        platform: 'android',
        app_version: '0.1.0',
        context: { screen: 'support', locale: 'pt' },
      },
    })
  })

  it('reads, answers and resolves one ticket, escaping the id it was given', async () => {
    await listTickets()
    expect(request).toHaveBeenCalledWith('/support/tickets', { signal: undefined })

    await getTicket('t 1')
    expect(request).toHaveBeenLastCalledWith('/support/tickets/t%201', { signal: undefined })

    await replyToTicket('t 1', 'Ainda está acontecendo.')
    expect(request).toHaveBeenLastCalledWith('/support/tickets/t%201/messages', {
      method: 'POST',
      body: { body: 'Ainda está acontecendo.' },
    })

    await resolveTicket('t 1')
    expect(request).toHaveBeenLastCalledWith('/support/tickets/t%201/resolve', { method: 'POST' })
  })

  it('lets a refusal from the Core through instead of interpreting it', async () => {
    request.mockRejectedValueOnce(new Error('409'))
    await expect(replyToTicket('t1', 'oi')).rejects.toThrow('409')
  })
})
