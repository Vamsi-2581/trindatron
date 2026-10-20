import { useEffect, useState } from 'react'
import { useAppStore } from './store/useAppStore'
import { getStoredToken, setToken, apiGetMe } from './api/client'
import AuthScreen from './components/AuthScreen'
import Sidebar from './components/Sidebar'
import ChatArea from './components/ChatArea'

export default function App() {
  const { token, user, setAuth } = useAppStore()
  const [booting, setBooting] = useState(true)

  useEffect(() => {
    const savedToken = getStoredToken()
    if (!savedToken) {
      setBooting(false)
      return
    }
    apiGetMe()
      .then((me) => setAuth(savedToken, { email: me.email, plan: me.plan }))
      .catch(() => setToken(null))
      .finally(() => setBooting(false))
  }, [])

  if (booting) {
    return (
      <div className="flex h-full items-center justify-center bg-surface-0">
        <span className="text-accent-light font-semibold tracking-widest text-sm animate-pulse">
          TRINDATRON
        </span>
      </div>
    )
  }

  if (!token || !user) {
    return <AuthScreen />
  }

  return (
    <div className="flex h-full bg-surface-0 text-gray-100 overflow-hidden">
      <Sidebar />
      <ChatArea />
    </div>
  )
}
