import { api } from './client.js'

export function createChangeReview(projectId, payload) {
  return api(`/api/projects/${projectId}/reviews`, {
    method: 'POST',
    body: payload,
  })
}

export function listChangeReviews(projectId) {
  return api(`/api/projects/${projectId}/reviews`)
}

export function getChangeReviewDetail(reviewId) {
  return api(`/api/reviews/${reviewId}`)
}

export function approveChangeReview(reviewId, payload = {}) {
  return api(`/api/reviews/${reviewId}/approve`, {
    method: 'POST',
    body: payload,
  })
}

export function rejectChangeReview(reviewId, payload = {}) {
  return api(`/api/reviews/${reviewId}/reject`, {
    method: 'POST',
    body: payload,
  })
}

export function exportReviewMarkdown(reviewId) {
  return api(`/api/reviews/${reviewId}/export?format=json`)
}

export function getRunTrace(runId) {
  return api(`/api/runs/${runId}/trace`)
}
