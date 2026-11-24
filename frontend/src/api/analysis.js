import { api } from './client.js'

export function listRulesCatalog() {
  return api('/api/analysis/rules')
}

export function runProjectAnalysis(projectId, payload = { run_llm: false }) {
  return api(`/api/projects/${projectId}/analyze`, {
    method: 'POST',
    body: payload,
  })
}

export function runScriptAnalysis(projectId, scriptId, payload = { run_llm: false }) {
  return api(`/api/projects/${projectId}/scripts/${scriptId}/analyze`, {
    method: 'POST',
    body: payload,
  })
}

export function listFindings(projectId, filters = {}) {
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(filters)) {
    if (v) params.set(k, v)
  }
  const qs = params.toString()
  return api(`/api/projects/${projectId}/findings${qs ? `?${qs}` : ''}`)
}

export function updateFindingStatus(findingId, status, resolution_note = '') {
  return api(`/api/findings/${findingId}`, {
    method: 'PATCH',
    body: { status, resolution_note },
  })
}
