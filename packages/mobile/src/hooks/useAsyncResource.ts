import { useFocusEffect } from 'expo-router'
import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react'

import { toApiError, type ApiError } from '@/services/api/errors'

export interface AsyncResource<T> {
  data: T | null
  error: ApiError | null
  loading: boolean
  refreshing: boolean
  refresh: () => Promise<void>
  setData: (updater: T | null | ((current: T | null) => T | null)) => void
}

interface Options<T> {
  /** Shown immediately (e.g. from the offline cache) while the Core answers. */
  initialData?: () => Promise<T | null>
  /** Persist fresh data (e.g. into the offline cache). */
  onData?: (data: T) => void
  refetchOnFocus?: boolean
}

export function useAsyncResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  options: Options<T> = {},
): AsyncResource<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)
  const loaderRef = useRef(loader)
  const optionsRef = useRef(options)
  const loadedOnce = useRef(false)

  useEffect(() => {
    loaderRef.current = loader
    optionsRef.current = options
  })

  const run = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    try {
      const result = await loaderRef.current(controller.signal)
      if (controller.signal.aborted) return
      setData(result)
      setError(null)
      loadedOnce.current = true
      optionsRef.current.onData?.(result)
    } catch (cause) {
      if (controller.signal.aborted) return
      const apiError = toApiError(cause)
      if (apiError.kind !== 'aborted') setError(apiError)
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    loadedOnce.current = false
    optionsRef.current.initialData?.().then((cached) => {
      if (!cancelled && cached !== null && !loadedOnce.current) {
        setData(cached)
        setLoading(false)
      }
    })
    void run()
    return () => {
      cancelled = true
      controllerRef.current?.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  const refresh = useCallback(() => {
    setRefreshing(true)
    return run()
  }, [run])

  useFocusEffect(
    useCallback(() => {
      if (optionsRef.current.refetchOnFocus && loadedOnce.current) void run()
    }, [run]),
  )

  return { data, error, loading, refreshing, refresh, setData }
}
