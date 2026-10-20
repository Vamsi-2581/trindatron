import { render } from '@testing-library/react-native'

import { completeGoogleSignIn, GoogleSignIn } from '@/components/auth/GoogleSignIn'
import * as google from '@/features/auth/google'
import { useAuthStore } from '@/stores/auth'

jest.mock('expo-web-browser', () => ({ maybeCompleteAuthSession: jest.fn() }))
jest.mock('expo-auth-session/providers/google', () => ({ useIdTokenAuthRequest: jest.fn(() => [null, null, jest.fn()]) }))
jest.mock('@/features/auth/google', () => ({
  ...jest.requireActual('@/features/auth/google'),
  exchangeGoogleIdToken: jest.fn(),
}))

const exchange = google.exchangeGoogleIdToken as jest.MockedFunction<typeof google.exchangeGoogleIdToken>
const ids = { android: 'android-client', ios: 'ios-client', web: null }
const NONCE = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'

beforeEach(() => {
  jest.clearAllMocks()
})

describe('Google sign-in availability', () => {
  it('uses the Core route that verifies a native ID token', () => {
    expect(google.CORE_GOOGLE_SIGN_IN_PATH).toBe('/auth/google/id-token')
  })

  it('stays hidden until this build carries an OAuth client id for its platform', () => {
    expect(google.isGoogleSignInAvailable('android', { android: null, ios: null, web: null })).toBe(false)
    // No client id is configured in the test environment, so nothing renders.
    expect(render(<GoogleSignIn />).toJSON()).toBeNull()
  })

  it('needs both the Core route and a client id for the platform', () => {
    expect(google.isGoogleSignInAvailable('android', ids, '/auth/google/id-token')).toBe(true)
    expect(google.isGoogleSignInAvailable('ios', ids, '/auth/google/id-token')).toBe(true)
    expect(google.isGoogleSignInAvailable('web', ids, '/auth/google/id-token')).toBe(false)
    expect(google.isGoogleSignInAvailable('android', ids, null)).toBe(false)
  })

  it('refuses to call an endpoint that does not exist', async () => {
    await expect(jest.requireActual<typeof google>('@/features/auth/google').exchangeGoogleIdToken('id-token', NONCE, null)).rejects.toMatchObject({
      kind: 'config',
    })
  })
})

describe('completing the OAuth flow', () => {
  const success = { type: 'success', params: { id_token: 'google-id-token' }, authentication: null, url: '', errorCode: null } as never

  it('treats cancelling or dismissing as a non-event', async () => {
    expect(await completeGoogleSignIn({ type: 'cancel' }, NONCE)).toBe('cancelled')
    expect(await completeGoogleSignIn({ type: 'dismiss' }, NONCE)).toBe('cancelled')
    expect(exchange).not.toHaveBeenCalled()
  })

  it('fails without an ID token, without the request nonce, or on an OAuth error', async () => {
    expect(await completeGoogleSignIn({ type: 'success', params: {}, authentication: null, url: '', errorCode: null } as never, NONCE)).toBe('failed')
    expect(await completeGoogleSignIn(success, null)).toBe('failed')
    expect(await completeGoogleSignIn({ type: 'error', params: {}, authentication: null, url: '', errorCode: 'x', error: null } as never, NONCE)).toBe('failed')
    expect(exchange).not.toHaveBeenCalled()
  })

  it('exchanges the ID token and its nonce for a persistent TRINDATRON session', async () => {
    exchange.mockResolvedValue({ access_token: 'trindatron-token', token_type: 'bearer', user_id: 'u1', email: 'ana@example.com', refresh_token: 'refresh-1' })
    const signInWithToken = jest.spyOn(useAuthStore.getState(), 'signInWithToken').mockResolvedValue(undefined)

    expect(await completeGoogleSignIn(success, NONCE)).toBe('signedIn')
    expect(exchange).toHaveBeenCalledWith('google-id-token', NONCE)
    expect(signInWithToken).toHaveBeenCalledWith('trindatron-token', 'refresh-1')
  })

  it('reports a failed exchange', async () => {
    exchange.mockRejectedValue(new Error('nope'))
    expect(await completeGoogleSignIn(success, NONCE)).toBe('failed')
  })
})
