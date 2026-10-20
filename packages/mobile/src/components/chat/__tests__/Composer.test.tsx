import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'
import { StyleSheet } from 'react-native'

import { baselineCapabilities } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import * as pickers from '@/features/files/pickers'
import * as voiceInput from '@/features/voice/useVoiceInput'
import * as attachmentsApi from '@/services/api/attachments'
import { errorFromResponse } from '@/services/api/errors'

import { Composer } from '../Composer'

jest.mock('@/features/voice/useVoiceInput', () => ({ useVoiceInput: jest.fn() }))
jest.mock('@/services/api/attachments')
jest.mock('@/features/files/pickers', () => ({ ...jest.requireActual('@/features/files/pickers'), pickDocument: jest.fn() }))

const useVoiceInput = voiceInput.useVoiceInput as jest.MockedFunction<typeof voiceInput.useVoiceInput>
const attachments = attachmentsApi as jest.Mocked<typeof attachmentsApi>
const pickDocument = pickers.pickDocument as jest.MockedFunction<typeof pickers.pickDocument>

function voice(overrides: Partial<ReturnType<typeof voiceInput.useVoiceInput>> = {}) {
  const value = { phase: 'idle' as const, error: null, durationMs: 0, start: jest.fn(), stop: jest.fn(), cancel: jest.fn(), clearError: jest.fn(), ...overrides }
  useVoiceInput.mockReturnValue(value)
  return value
}

const pdf: pickers.PickedFile = {
  kind: 'document',
  uri: 'file:///cache/relatorio.pdf',
  name: 'relatorio.pdf',
  mimeType: 'application/pdf',
  file: null,
  previewUri: null,
  sizeBytes: 48_000,
}

const attached: attachmentsApi.SessionAttachment = {
  id: 'a1',
  session_id: 's1',
  name: 'relatorio.pdf',
  mime_type: 'application/pdf',
  size_bytes: 48_000,
  kind: 'document',
  project_file_id: null,
  readable: true,
  created_at: '2026-09-14T10:00:00',
  content_path: '/sessions/s1/attachments/a1/content',
}

beforeEach(() => {
  jest.resetAllMocks()
  voice()
  attachments.listAttachments.mockResolvedValue({ attachments: [] })
  useCapabilityStore.setState({ capabilities: baselineCapabilities(), loaded: true })
})

async function pickTheDocument() {
  fireEvent.press(screen.getByLabelText('Adicionar'))
  await act(async () => {
    fireEvent.press(await screen.findByLabelText('Documento, Anexa a esta conversa'))
  })
}

describe('Composer', () => {
  it('sends the typed text and clears the field', () => {
    const onSend = jest.fn()
    render(<Composer onSend={onSend} streaming={false} />)

    fireEvent.changeText(screen.getByLabelText('Mensagem para o TRINDATRON'), 'Resuma o relatório')
    fireEvent.press(screen.getByLabelText('Enviar mensagem'))

    expect(onSend).toHaveBeenCalledWith('Resuma o relatório')
    expect(screen.getByLabelText('Mensagem para o TRINDATRON').props.value).toBe('')
  })

  it('puts the text back when the message could not be started', async () => {
    render(<Composer onSend={() => Promise.resolve(false)} streaming={false} />)
    fireEvent.changeText(screen.getByLabelText('Mensagem para o TRINDATRON'), 'Plano de marketing')
    await act(async () => {
      fireEvent.press(screen.getByLabelText('Enviar mensagem'))
    })
    expect(screen.getByLabelText('Mensagem para o TRINDATRON').props.value).toBe('Plano de marketing')
  })

  it('always offers add, text and microphone — the mic stays even while typing', () => {
    render(<Composer onSend={jest.fn()} streaming={false} />)
    fireEvent.changeText(screen.getByLabelText('Mensagem para o TRINDATRON'), 'Olá')
    expect(screen.getByLabelText('Adicionar')).toBeTruthy()
    expect(screen.getByLabelText('Gravar mensagem de voz')).toBeTruthy()
    expect(screen.getByLabelText('Enviar mensagem')).toBeTruthy()
  })

  it('has no cancel or stop action while TRINDATRON is replying', () => {
    render(<Composer onSend={jest.fn()} streaming />)

    expect(screen.queryByLabelText(/parar resposta|cancelar|stop/i)).toBeNull()
    const send = screen.getByLabelText('O TRINDATRON está respondendo')
    expect(send.props.accessibilityState).toMatchObject({ disabled: true, busy: true })
    expect(screen.getByLabelText('Gravar mensagem de voz').props.accessibilityState).toMatchObject({ disabled: true })
  })

  it('records with a visible state, and can be sent for transcription or discarded', () => {
    const recording = voice({ phase: 'recording', durationMs: 12_000 })
    render(<Composer onSend={jest.fn()} streaming={false} />)

    expect(screen.getByText('0:12')).toBeTruthy()
    expect(screen.getByText('Gravando…')).toBeTruthy()
    fireEvent.press(screen.getByLabelText('Descartar gravação'))
    expect(recording.cancel).toHaveBeenCalled()
    fireEvent.press(screen.getByLabelText('Parar gravação'))
    expect(recording.stop).toHaveBeenCalled()
  })

  it('shows transcription progress instead of the microphone while the Core transcribes', () => {
    voice({ phase: 'transcribing' })
    render(<Composer onSend={jest.fn()} streaming={false} />)
    expect(screen.getByLabelText('Transcrevendo áudio')).toBeTruthy()
  })

  it('offers camera, gallery and documents, attached to the conversation', async () => {
    renderRouter({ index: () => <Composer onSend={jest.fn()} streaming={false} sessionId="s1" /> }, { initialUrl: '/' })
    fireEvent.press(screen.getByLabelText('Adicionar'))

    expect(await screen.findByLabelText('Câmera, Anexa a esta conversa')).toBeTruthy()
    expect(screen.getByLabelText('Galeria, Anexa a esta conversa')).toBeTruthy()
    expect(screen.getByLabelText('Documento, Anexa a esta conversa')).toBeTruthy()
    // No creation shortcuts while the Core offers no media capability.
    expect(screen.queryByText('Criar imagem')).toBeNull()
    expect(screen.queryByText('Criar vídeo')).toBeNull()
  })

  it('counts a file as attached only when the Core returns it, and holds the message until then', async () => {
    let finishUpload: (value: attachmentsApi.SessionAttachment) => void = () => undefined
    attachments.attachUpload.mockReturnValue(new Promise((resolve) => (finishUpload = resolve)))
    pickDocument.mockResolvedValue(pdf)
    renderRouter({ index: () => <Composer onSend={jest.fn()} streaming={false} sessionId="s1" /> }, { initialUrl: '/' })

    fireEvent.changeText(screen.getByLabelText('Mensagem para o TRINDATRON'), 'Resuma o PDF')
    await pickTheDocument()

    expect(screen.getByText('Enviando… 0%')).toBeTruthy()
    expect(screen.queryByText('relatorio.pdf')).toBeNull()
    expect(screen.getByLabelText('Enviar mensagem').props.accessibilityState).toMatchObject({ disabled: true })
    expect(attachments.attachUpload).toHaveBeenCalledWith('s1', pdf, expect.objectContaining({ signal: expect.anything() }))

    await act(async () => finishUpload(attached))

    expect(await screen.findByText('relatorio.pdf')).toBeTruthy()
    expect(screen.getByLabelText('Enviar mensagem').props.accessibilityState).toMatchObject({ disabled: false })
  })

  it('keeps a refused upload visible with its reason, and retries it', async () => {
    attachments.attachUpload
      .mockRejectedValueOnce(errorFromResponse(404, { detail: { error: 'not_found', message: 'Not found.' } }))
      .mockResolvedValueOnce(attached)
    pickDocument.mockResolvedValue(pdf)
    renderRouter({ index: () => <Composer onSend={jest.fn()} streaming={false} sessionId="s1" /> }, { initialUrl: '/' })

    await pickTheDocument()

    expect(await screen.findByText('Não encontramos o que você procurava.')).toBeTruthy()
    await act(async () => {
      fireEvent.press(screen.getByLabelText('Tentar enviar relatorio.pdf de novo'))
    })
    await waitFor(() => expect(screen.queryByText('Não encontramos o que você procurava.')).toBeNull())
    expect(screen.getByText('relatorio.pdf')).toBeTruthy()
    expect(attachments.attachUpload).toHaveBeenCalledTimes(2)
  })

  it('refuses a file over the limit without uploading it', async () => {
    pickDocument.mockResolvedValue({ ...pdf, sizeBytes: 11 * 1024 * 1024 })
    renderRouter({ index: () => <Composer onSend={jest.fn()} streaming={false} sessionId="s1" /> }, { initialUrl: '/' })

    await pickTheDocument()

    expect(await screen.findByText('O arquivo é maior do que o limite permitido.')).toBeTruthy()
    expect(attachments.attachUpload).not.toHaveBeenCalled()
  })

  it('removes an attached file from the conversation', async () => {
    attachments.listAttachments.mockResolvedValue({ attachments: [attached] })
    attachments.removeAttachment.mockResolvedValue(undefined)
    render(<Composer onSend={jest.fn()} streaming={false} sessionId="s1" />)

    expect(await screen.findByText('relatorio.pdf')).toBeTruthy()
    await act(async () => {
      fireEvent.press(screen.getByLabelText('Remover relatorio.pdf'))
    })
    expect(attachments.removeAttachment).toHaveBeenCalledWith('s1', 'a1')
    expect(screen.queryByText('relatorio.pdf')).toBeNull()
  })

  it('shows creation shortcuts only for capabilities the Core reports, and opens the create screen with what was typed', async () => {
    const capabilities = baselineCapabilities()
    capabilities.image_generation = { available: true, dailyQuota: null, limits: null }
    capabilities.image_editing = { available: true, dailyQuota: null, limits: null }
    useCapabilityStore.setState({ capabilities })
    const app = renderRouter({ index: () => <Composer onSend={jest.fn()} streaming={false} />, create: () => null }, { initialUrl: '/' })

    fireEvent.changeText(screen.getByLabelText('Mensagem para o TRINDATRON'), 'um farol ao entardecer')
    fireEvent.press(screen.getByLabelText('Adicionar'))
    fireEvent.press(await screen.findByText('Criar imagem'))
    await waitFor(() => expect(screen).toHavePathname('/create'))
    expect(app.getSearchParams()).toEqual({ kind: 'image', prompt: 'um farol ao entardecer' })
    // The create screen cannot pick a source image yet, so editing is not offered.
    expect(screen.queryByText('Editar imagem')).toBeNull()
    expect(screen.queryByText('Criar vídeo')).toBeNull()
  })

  it('keeps long multiline text inside a scrollable, height-capped field', () => {
    render(<Composer onSend={jest.fn()} streaming={false} />)
    const input = screen.getByLabelText('Mensagem para o TRINDATRON')
    const long = Array.from({ length: 40 }, (_, line) => `linha ${line}`).join('\n')

    fireEvent.changeText(input, long)

    expect(input.props.value).toBe(long)
    expect(input.props.multiline).toBe(true)
    expect(input.props.scrollEnabled).toBe(true)
    expect(StyleSheet.flatten(input.props.style).maxHeight).toBeGreaterThan(0)
    expect(screen.getByLabelText('Enviar mensagem').props.accessibilityState).toMatchObject({ disabled: false })
  })

  it('reserves the bottom inset only when asked (keyboard closed)', () => {
    const rootStyle = (tree: unknown) =>
      StyleSheet.flatten((tree as { props: { style: object } }).props.style) as { paddingBottom: number }
    const { rerender, toJSON } = render(<Composer onSend={jest.fn()} streaming={false} bottomInset={34} />)
    const withInset = rootStyle(toJSON())
    rerender(<Composer onSend={jest.fn()} streaming={false} bottomInset={0} />)
    const withoutInset = rootStyle(toJSON())
    expect(withInset.paddingBottom - withoutInset.paddingBottom).toBe(34)
  })
})
