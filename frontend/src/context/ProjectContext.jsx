import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { listProjects } from '../api/workspace.js'
import { fetchDashboard, fetchHealth } from '../api/platform.js'

const ProjectContext = createContext(null)

export function ProjectProvider({ children }) {
  const [projects, setProjects] = useState([])
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const [dashboard, setDashboard] = useState(null)
  const [backendOnline, setBackendOnline] = useState(true)
  const [loadingProjects, setLoadingProjects] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [toasts, setToasts] = useState([])
  const toastSeq = useRef(0)

  const dismissToast = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const pushToast = useCallback((type, message) => {
    const id = ++toastSeq.current
    setToasts((prev) => [...prev.slice(-3), { id, type, message }])
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, 4800)
  }, [])

  const refreshProjects = useCallback(async (preferId) => {
    setLoadingProjects(true)
    try {
      const [healthRes, projRes, dashRes] = await Promise.all([
        fetchHealth(),
        listProjects(),
        fetchDashboard(),
      ])
      setBackendOnline(healthRes?.status === 'ok')
      const list = projRes?.projects || []
      setProjects(list)
      setDashboard(dashRes)
      setSelectedProjectId((curr) => {
        if (preferId !== undefined) {
          if (preferId === '') return list[0]?.id || ''
          if (list.some((p) => p.id === preferId)) return preferId
        }
        if (curr && list.some((p) => p.id === curr)) return curr
        return list[0]?.id || ''
      })
    } catch {
      setBackendOnline(false)
    } finally {
      setLoadingProjects(false)
    }
  }, [])

  useEffect(() => {
    refreshProjects().catch(() => {})
  }, [refreshProjects])

  return (
    <ProjectContext.Provider
      value={{
        projects,
        selectedProjectId,
        setSelectedProjectId,
        dashboard,
        backendOnline,
        loadingProjects,
        refreshProjects,
        importModalOpen,
        setImportModalOpen,
        toasts,
        pushToast,
        dismissToast,
      }}
    >
      {children}
    </ProjectContext.Provider>
  )
}

export function useProjectContext() {
  const ctx = useContext(ProjectContext)
  if (!ctx) throw new Error('useProjectContext must be used inside ProjectProvider')
  return ctx
}
