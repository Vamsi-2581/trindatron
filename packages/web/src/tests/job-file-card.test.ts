import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createElement } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  apiGetExecutionJob: vi.fn(),
  apiListExecutionArtifacts: vi.fn(),
  apiGetExecutionArtifactLink: vi.fn(),
  apiFetchExecutionArtifactBlob: vi.fn(),
}))
vi.mock('@/api/client', () => api)

import { JobFileCard } from '@/components/chat/job-file-card'

const deck = { id: 'a1', filename: 'presentation.pptx', content_type: 'application/pptx', size_bytes: 442_093 }

function job(status: string) {
  return { id: 'j1', job_type: 'presentation', status, error: null }
}

describe('JobFileCard — a deck asked for in the chat', () => {
  beforeEach(() => {
    Object.values(api).forEach((fn) => fn.mockReset())
  })

  it('says it is being made while the job runs', async () => {
    api.apiGetExecutionJob.mockResolvedValue(job('running'))
    render(createElement(JobFileCard, { jobId: 'j1' }))
    expect(await screen.findByText(/Criando sua apresentação/)).toBeInTheDocument()
    expect(api.apiListExecutionArtifacts).not.toHaveBeenCalled()
  })

  it('offers the finished file, with its size', async () => {
    api.apiGetExecutionJob.mockResolvedValue(job('completed'))
    api.apiListExecutionArtifacts.mockResolvedValue([deck])
    render(createElement(JobFileCard, { jobId: 'j1' }))
    expect(await screen.findByText('presentation.pptx')).toBeInTheDocument()
    expect(screen.getByText(/442 KB/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Baixar/ })).toBeEnabled()
  })

  it('downloads through the short-lived link, never a URL built by the client', async () => {
    api.apiGetExecutionJob.mockResolvedValue(job('completed'))
    api.apiListExecutionArtifacts.mockResolvedValue([deck])
    api.apiGetExecutionArtifactLink.mockResolvedValue({ url: 'https://storage.example/signed', filename: deck.filename })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(createElement(JobFileCard, { jobId: 'j1' }))
    fireEvent.click(await screen.findByRole('button', { name: /Baixar/ }))
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(api.apiGetExecutionArtifactLink).toHaveBeenCalledWith('a1')
    expect(api.apiFetchExecutionArtifactBlob).not.toHaveBeenCalled()
    click.mockRestore()
  })

  it('reads the file with the session where storage cannot sign a link', async () => {
    api.apiGetExecutionJob.mockResolvedValue(job('completed'))
    api.apiListExecutionArtifacts.mockResolvedValue([deck])
    api.apiGetExecutionArtifactLink.mockResolvedValue({ url: null, filename: deck.filename })
    api.apiFetchExecutionArtifactBlob.mockResolvedValue(new Blob(['PK']))
    URL.createObjectURL = vi.fn(() => 'blob:deck')
    URL.revokeObjectURL = vi.fn()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(createElement(JobFileCard, { jobId: 'j1' }))
    fireEvent.click(await screen.findByRole('button', { name: /Baixar/ }))
    await waitFor(() => expect(api.apiFetchExecutionArtifactBlob).toHaveBeenCalledWith('a1'))
    await waitFor(() => expect(click).toHaveBeenCalled())
    click.mockRestore()
  })

  it('says plainly when the deck could not be made', async () => {
    api.apiGetExecutionJob.mockResolvedValue(job('failed'))
    render(createElement(JobFileCard, { jobId: 'j1' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Não foi possível criar a apresentação/)
    expect(api.apiListExecutionArtifacts).not.toHaveBeenCalled()
  })
})
