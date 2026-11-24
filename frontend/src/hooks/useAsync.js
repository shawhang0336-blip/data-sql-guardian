import { useCallback, useState } from 'react'

export function useAsync() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const run = useCallback(async (fn) => {
    setBusy(true)
    setError('')
    try {
      return await fn()
    } catch (err) {
      setError(err?.message || '操作失败')
      throw err
    } finally {
      setBusy(false)
    }
  }, [])

  return { busy, error, setError, run }
}
