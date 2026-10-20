import { act, fireEvent, screen, waitFor } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'

import { baselineCapabilities } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import * as creationFile from '@/features/media/creationFile'
import type { MediaCreation } from '@/features/media/media'

import { MediaActions } from '../MediaActions'
import { MediaResultCard } from '../MediaResultCard'

jest.mock('@/features/media/creationFile', () => ({ saveCreation: jest.fn(), shareCreation: jest.fn(), creationViewSource: jest.fn() }))

const files = creationFile as jest.Mocked<typeof creationFile>

const finished: MediaCreation = {
  id: 'j1',
  kind: 'image',
  status: 'completed',
  progress: 1,
  previewUrl: null,
  downloadUrl: null,
  contentPath: '/media/artifacts/a1/content',
  downloadPath: '/media/artifacts/a1/download',
  contentType: 'image/png',
  createdAt: '2026-09-15T10:00:00Z',
}

function show(node: React.ReactElement) {
  return renderRouter({ index: () => node, create: () => null, 'creation/[jobId]': () => null }, { initialUrl: '/' })
}

function creationAvailable(available: boolean) {
  const capabilities = baselineCapabilities()
  for (const id of ['image_generation', 'video_generation'] as const) {
    capabilities[id] = { available, status: available ? 'available' : 'unavailable', dailyQuota: null, limits: null }
  }
  useCapabilityStore.setState({ capabilities, loaded: true })
}

beforeEach(() => {
  jest.resetAllMocks()
  creationAvailable(true)
})

describe('creation actions', () => {
  it('offers open, download, share and create another for a finished creation', () => {
    show(<MediaActions media={finished} />)
    for (const label of ['Abrir', 'Baixar', 'Compartilhar', 'Criar outra']) expect(screen.getByText(label)).toBeTruthy()
  })

  it('does not offer to create another where the Core cannot create one — what exists stays usable', () => {
    creationAvailable(false)
    show(<MediaActions media={finished} />)
    for (const label of ['Abrir', 'Baixar', 'Compartilhar']) expect(screen.getByText(label)).toBeTruthy()
    expect(screen.queryByText('Criar outra')).toBeNull()
  })

  it('offers nothing while the creation is still being made', () => {
    show(<MediaActions media={{ ...finished, status: 'running', downloadPath: null }} />)
    expect(screen.queryByText('Baixar')).toBeNull()
    expect(screen.queryByText('Criar outra')).toBeNull()
  })

  it('offers only create another when the creation failed', () => {
    show(<MediaActions media={{ ...finished, status: 'failed', contentPath: null, downloadPath: null }} />)
    expect(screen.queryByText('Abrir')).toBeNull()
    expect(screen.queryByText('Compartilhar')).toBeNull()
    expect(screen.getByText('Criar outra')).toBeTruthy()
  })

  it('opens the creation full size', async () => {
    show(<MediaActions media={finished} />)
    fireEvent.press(screen.getByText('Abrir'))
    await waitFor(() => expect(screen).toHavePathname('/creation/j1'))
  })

  it('shares the file itself', async () => {
    files.shareCreation.mockResolvedValue('shared')
    show(<MediaActions media={finished} />)
    await act(async () => {
      fireEvent.press(screen.getByText('Compartilhar'))
    })
    expect(files.shareCreation).toHaveBeenCalledWith(finished)
    expect(screen.queryByText('Não foi possível concluir agora. Tente de novo.')).toBeNull()
  })

  it('confirms a saved file, stays quiet when the person cancels, and reports a failure without detail', async () => {
    files.saveCreation.mockResolvedValueOnce('saved').mockResolvedValueOnce('cancelled').mockRejectedValueOnce(new Error('HTTP 500 storage'))
    show(<MediaActions media={finished} />)

    await act(async () => {
      fireEvent.press(screen.getByText('Baixar'))
    })
    expect(screen.getByText('Arquivo salvo.')).toBeTruthy()

    await act(async () => {
      fireEvent.press(screen.getByText('Baixar'))
    })
    expect(screen.queryByText('Arquivo salvo.')).toBeNull()

    await act(async () => {
      fireEvent.press(screen.getByText('Baixar'))
    })
    expect(screen.getByText('Não foi possível concluir agora. Tente de novo.')).toBeTruthy()
    expect(screen.queryByText(/500|storage/)).toBeNull()
  })

  it('starts another creation of the same kind', async () => {
    const app = show(<MediaActions media={{ ...finished, kind: 'video', contentType: 'video/mp4' }} />)
    fireEvent.press(screen.getByText('Criar outra'))
    await waitFor(() => expect(screen).toHavePathname('/create'))
    expect(app.getSearchParams()).toEqual({ kind: 'video' })
  })

  it('shows the same actions on a creation card rebuilt from history', () => {
    show(<MediaResultCard media={finished} />)
    for (const label of ['Abrir', 'Baixar', 'Compartilhar', 'Criar outra']) expect(screen.getByText(label)).toBeTruthy()
  })
})
