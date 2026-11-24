import { api } from './client.js'

export function fetchHealth() {
  return api('/api/system/health')
}

export function fetchDashboard() {
  return api('/api/system/dashboard')
}

export function fetchSettings() {
  return api('/api/settings')
}

export function updateLlmSettings(payload) {
  return api('/api/settings/llm', { method: 'PUT', body: payload })
}

export function updateBudgetSettings(payload) {
  return api('/api/settings/budget', { method: 'PUT', body: payload })
}

export function testLlmSettings() {
  return api('/api/settings/llm/test', { method: 'POST' })
}
