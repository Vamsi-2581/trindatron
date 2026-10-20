import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'
import { AccessibilityInfo, Image, Text } from 'react-native'

import { BrandIntro, INTRO_MAX_MS } from '@/components/brand/BrandIntro'
import { BRAND_ASSETS } from '@/components/ui/Brand'
import * as tokenStorage from '@/services/session/tokenStorage'
import { useAuthStore } from '@/stores/auth'

import RootLayout from '../../app/_layout'
import AuthLayout from '../../app/(auth)/_layout'
import LoginScreen from '../../app/(auth)/login'

jest.unmock('@/components/brand/BrandIntro')
jest.mock('@/services/session/tokenStorage')
jest.mock('expo-web-browser', () => ({ maybeCompleteAuthSession: jest.fn() }))

const storage = tokenStorage as jest.Mocked<typeof tokenStorage>

afterEach(() => {
  jest.useRealTimers()
  jest.restoreAllMocks()
})

describe('opening animation', () => {
  it('draws only the official brand files and ignores touches', () => {
    render(<BrandIntro onDone={jest.fn()} />)
    const sources = screen.UNSAFE_getAllByType(Image).map((image) => image.props.source)
    expect(sources).toEqual(expect.arrayContaining(Object.values(BRAND_ASSETS)))
    expect(screen.getByTestId('brand-intro').props.pointerEvents).toBe('none')
    expect(screen.getByLabelText('TRINDATRON')).toBeTruthy()
  })

  it('waits for its artwork before playing, then takes over from the native splash exactly once', async () => {
    jest.spyOn(AccessibilityInfo, 'isReduceMotionEnabled').mockResolvedValue(false)
    const onReady = jest.fn()
    render(<BrandIntro onDone={jest.fn()} onReady={onReady} />)
    await act(async () => undefined)
    expect(onReady).not.toHaveBeenCalled()

    for (const image of screen.UNSAFE_getAllByType(Image)) fireEvent(image, 'load')
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1))
  })

  it('reveals the app by its hard ceiling even if the animation never runs', () => {
    jest.useFakeTimers()
    jest.spyOn(AccessibilityInfo, 'isReduceMotionEnabled').mockReturnValue(new Promise<boolean>(() => undefined))
    const onDone = jest.fn()
    render(<BrandIntro onDone={onDone} />)
    act(() => {
      jest.advanceTimersByTime(INTRO_MAX_MS)
    })
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('falls back at once when an asset cannot be drawn, and finishes only once', () => {
    jest.useFakeTimers()
    const onDone = jest.fn()
    render(<BrandIntro onDone={onDone} />)
    fireEvent(screen.UNSAFE_getAllByType(Image)[0]!, 'error')
    act(() => {
      jest.advanceTimersByTime(INTRO_MAX_MS)
    })
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('with reduced motion shows the still identity and finishes quickly', async () => {
    jest.spyOn(AccessibilityInfo, 'isReduceMotionEnabled').mockResolvedValue(true)
    const onDone = jest.fn()
    render(<BrandIntro onDone={onDone} />)
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { timeout: 1500 })
  })

  it('does not block startup: the session boots and the sign-in screen is ready underneath', async () => {
    // Hold the animation at its first frame: the sign-in screen must be ready while the intro is still on screen.
    jest.spyOn(AccessibilityInfo, 'isReduceMotionEnabled').mockReturnValue(new Promise<boolean>(() => undefined))
    storage.readToken.mockResolvedValue({ status: 'absent' })
    storage.deleteToken.mockResolvedValue(undefined)
    useAuthStore.setState({ status: 'booting', token: null, user: null, notice: null, storageUnavailable: false })

    renderRouter(
      {
        _layout: RootLayout,
        '(auth)/_layout': AuthLayout,
        '(auth)/login': LoginScreen,
        '(auth)/register': () => <Text>register</Text>,
      },
      { initialUrl: '/login' },
    )

    await waitFor(() => expect(screen.getByLabelText('E-mail')).toBeTruthy())
    expect(screen.queryByTestId('brand-intro')).toBeTruthy()
  })
})
