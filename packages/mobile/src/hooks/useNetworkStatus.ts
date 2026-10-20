import NetInfo from '@react-native-community/netinfo'
import { useEffect, useState } from 'react'

/** True only when the OS is sure there is no connection (unknown counts as online). */
export function useIsOffline(): boolean {
  const [offline, setOffline] = useState(false)
  useEffect(
    () =>
      NetInfo.addEventListener((state) => {
        setOffline(state.isConnected === false || state.isInternetReachable === false)
      }),
    [],
  )
  return offline
}
