import React, { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { listProjects } from '../api/workspace.js'
import { fetchDashboard } from '../api/platform.js'

const ProjectContext = createContext(null)

export function ProjectProvider({ children }) {
  const [projects, setProjects] = useState([])
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const [dashboard, setDashboard] = useState(null)
  const [loadingProjects, setLoadingProjects] = useState(false)

  const refreshProjects = useCallback(async (preferId) => {
    setLoadingProjects(true)
    try {
      const [projRes, dashRes] = await Promise.all([listProjects(), fetchDashboard()])
      const list = projRes?.projects || []
      setProjects(list)
      setDashboard(dashRes)
      setSelectedProjectId((curr) => {
        if (preferId && list.some((p) => p.id === preferId)) return preferId
        if (curr && list.some((p) => p.id === curr)) return curr
        return list[0]?.id || ''
      })
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
        loadingProjects,
        refreshProjects,
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
