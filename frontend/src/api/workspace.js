import { api } from './client.js'

export function listProjects() {
  return api('/api/projects')
}

export function importSacProject(name) {
  return api('/api/projects/import-sac', {
    method: 'POST',
    body: name ? { name } : {},
  })
}

export function importDirProject(payload) {
  return api('/api/projects/import-dir', {
    method: 'POST',
    body: payload,
  })
}

export function importZipProject(file, name, dialect = 'auto') {
  const form = new FormData()
  form.append('file', file)
  form.append('name', name || 'ZIP 导入项目')
  form.append('dialect', dialect)
  return api('/api/projects/import-zip', {
    method: 'POST',
    body: form,
  })
}

export function getProjectDetail(projectId) {
  return api(`/api/projects/${projectId}`)
}

export function deleteProject(projectId) {
  return api(`/api/projects/${projectId}`, { method: 'DELETE' })
}

export function getScriptDetail(projectId, scriptId) {
  return api(`/api/projects/${projectId}/scripts/${scriptId}`)
}

export function listProjectSchemas(projectId) {
  return api(`/api/projects/${projectId}/schemas`)
}

export function getProjectLineage(projectId) {
  return api(`/api/projects/${projectId}/lineage`)
}

export function rebuildProjectLineage(projectId) {
  return api(`/api/projects/${projectId}/lineage/rebuild`, { method: 'POST' })
}
