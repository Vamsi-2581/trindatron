import { act, fireEvent, render, screen } from '@testing-library/react-native'

import { baselineCapabilities } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import { ContinueAnswerContext } from '@/features/chat/continueAnswer'
import type { ChatMessage } from '@/features/chat/chatReducer'
import { useLocaleStore } from '@/i18n'
import * as voiceApi from '@/services/api/voice'

import { MessageBubble } from '../MessageBubble'

jest.mock('@/services/api/voice')
const synthesize = voiceApi.synthesizeSpeech as jest.MockedFunction<typeof voiceApi.synthesizeSpeech>

const cut: ChatMessage = {
  id: 'm2',
  role: 'assistant',
  content: '| Hora | Atividade |\n|---|---|\n| 22:00 – 22',
  createdAt: '2026-09-17T14:49:00Z',
  state: 'final',
  truncated: true,
}

function renderBubble(message: ChatMessage, { latest, onContinue }: { latest: boolean; onContinue: (() => void) | null }) {
  return render(
    <ContinueAnswerContext.Provider value={onContinue}>
      <MessageBubble message={message} activity={null} agentSteps={[]} latest={latest} />
    </ContinueAnswerContext.Provider>,
  )
}

beforeEach(() => useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' }))

describe('MessageBubble — an answer cut at the plan limit', () => {
  it('says so, and the newest one can be continued', () => {
    const onContinue = jest.fn()
    renderBubble(cut, { latest: true, onContinue })
    expect(screen.getByText('A resposta parou no limite de tamanho do seu plano.')).toBeTruthy()
    fireEvent.press(screen.getByRole('button', { name: 'Continuar' }))
    expect(onContinue).toHaveBeenCalledTimes(1)
  })

  it('offers no continue on an older answer, or while another reply is in progress', () => {
    renderBubble(cut, { latest: false, onContinue: jest.fn() })
    expect(screen.getByText('A resposta parou no limite de tamanho do seu plano.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Continuar' })).toBeNull()

    screen.unmount()
    renderBubble(cut, { latest: true, onContinue: null })
    expect(screen.queryByRole('button', { name: 'Continuar' })).toBeNull()
  })

  it('shows nothing on a finished answer', () => {
    renderBubble({ ...cut, truncated: undefined, content: 'Pronto.' }, { latest: true, onContinue: jest.fn() })
    expect(screen.queryByText('A resposta parou no limite de tamanho do seu plano.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Continuar' })).toBeNull()
  })
})

describe('MessageBubble — hearing an answer is up to the person', () => {
  beforeEach(() => {
    synthesize.mockReset().mockResolvedValue({ audio: new ArrayBuffer(8), contentType: 'audio/mpeg', shortened: false })
    const capabilities = baselineCapabilities()
    capabilities.audio_output = { available: true, status: 'available', dailyQuota: null, limits: null }
    useCapabilityStore.setState({ capabilities, loaded: true })
  })

  it('an answer that arrives is never read on its own', () => {
    renderBubble({ ...cut, truncated: undefined, content: 'Pronto.' }, { latest: true, onContinue: null })
    expect(screen.getByRole('button', { name: 'Ouvir' })).toBeTruthy()
    expect(synthesize).not.toHaveBeenCalled()
  })

  it('reads it only when "Ouvir" is tapped', async () => {
    renderBubble({ ...cut, truncated: undefined, content: 'Pronto.' }, { latest: true, onContinue: null })
    await act(async () => {
      fireEvent.press(screen.getByRole('button', { name: 'Ouvir' }))
    })
    expect(synthesize).toHaveBeenCalledTimes(1)
    expect(synthesize).toHaveBeenCalledWith('Pronto.')
  })
})
