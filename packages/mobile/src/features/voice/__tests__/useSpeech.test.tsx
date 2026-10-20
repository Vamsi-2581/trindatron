import { act, render } from '@testing-library/react-native'
import { createAudioPlayer } from 'expo-audio'
import { Text } from 'react-native'

import * as voiceApi from '@/services/api/voice'

import { useSpeech } from '../useSpeech'

jest.mock('@/services/api/voice')

const synthesize = voiceApi.synthesizeSpeech as jest.MockedFunction<typeof voiceApi.synthesizeSpeech>
const createPlayer = createAudioPlayer as jest.Mock

function player() {
  const listeners: ((status: { didJustFinish: boolean }) => void)[] = []
  return {
    play: jest.fn(),
    pause: jest.fn(),
    remove: jest.fn(),
    seekTo: jest.fn(async () => undefined),
    addListener: jest.fn((_event: string, listener: (status: { didJustFinish: boolean }) => void) => {
      listeners.push(listener)
      return { remove: jest.fn() }
    }),
    finish: () => listeners.forEach((listener) => listener({ didJustFinish: true })),
  }
}

const speech = (overrides: Partial<voiceApi.SynthesizedSpeech> = {}): voiceApi.SynthesizedSpeech => ({
  audio: new ArrayBuffer(8),
  contentType: 'audio/mpeg',
  shortened: false,
  ...overrides,
})

/** A probe that exposes the hook to the test without a screen around it. */
function Speaker({ onReady }: { onReady: (speech: ReturnType<typeof useSpeech>) => void }) {
  const speech = useSpeech()
  onReady(speech)
  return <Text>{speech.phase}</Text>
}

function mount() {
  let latest!: ReturnType<typeof useSpeech>
  const view = render(<Speaker onReady={(speech) => (latest = speech)} />)
  return { view, speech: () => latest }
}

describe('hearing an answer', () => {
  beforeEach(() => {
    jest.clearAllMocks()
    synthesize.mockResolvedValue(speech())
    createPlayer.mockImplementation(player)
  })

  it('synthesises once and replays the same audio', async () => {
    const { speech } = mount()

    await act(async () => void (await speech().toggle('bom dia')))
    expect(synthesize).toHaveBeenCalledTimes(1)
    expect(speech().phase).toBe('playing')

    // Tapping again stops it; tapping once more replays without paying for a second synthesis.
    await act(async () => void (await speech().toggle('bom dia')))
    expect(speech().phase).toBe('idle')
    await act(async () => void (await speech().toggle('bom dia')))
    expect(synthesize).toHaveBeenCalledTimes(1)
    expect(speech().phase).toBe('playing')
  })

  it('a different answer replaces the first instead of talking over it', async () => {
    const first = player()
    const second = player()
    createPlayer.mockReturnValueOnce(first).mockReturnValueOnce(second)

    const a = mount()
    const b = mount()
    await act(async () => void (await a.speech().toggle('primeira')))
    await act(async () => void (await b.speech().toggle('segunda')))

    expect(first.pause).toHaveBeenCalled()
    expect(second.play).toHaveBeenCalled()
  })

  it('goes back to idle when the reading ends, instead of staying on "listening"', async () => {
    const created = player()
    createPlayer.mockReturnValue(created)
    const { speech: current } = mount()
    await act(async () => void (await current().toggle('fim')))
    expect(current().phase).toBe('playing')
    act(() => created.finish())
    expect(current().phase).toBe('idle')
  })

  it('plays what the Core sent as what it is, and says when only the start was read', async () => {
    synthesize.mockResolvedValue(speech({ contentType: 'audio/wav', shortened: true }))
    const { speech: current } = mount()
    await act(async () => void (await current().toggle('uma resposta longa')))
    expect(createPlayer).toHaveBeenCalledWith({ uri: expect.stringMatching(/^data:audio\/wav;base64,/) })
    expect(current().shortened).toBe(true)
  })

  it('leaving the screen releases the player', async () => {
    const created = player()
    createPlayer.mockReturnValue(created)
    const { view, speech } = mount()

    await act(async () => void (await speech().toggle('tchau')))
    act(() => view.unmount())

    expect(created.remove).toHaveBeenCalled()
  })

  it('a refusal is reported and never leaves it pretending to play', async () => {
    synthesize.mockRejectedValue(new Error('nope'))
    const { speech } = mount()

    await act(async () => void (await speech().toggle('erro')))

    expect(speech().phase).toBe('idle')
    expect(speech().error).toBeTruthy()
  })
})
