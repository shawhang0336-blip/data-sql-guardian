/**
 * Shared HTTP client strictly isolating all fetch() and /api/ calls inside src/api/.
 */

export class ApiError extends Error {
  constructor(status, detail) {
    super(formatApiErrorDetail(detail, status))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export function formatApiErrorDetail(detail, status) {
  if (!detail) return `请求失败（${status}）`
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const items = detail
      .map((item) => {
        if (typeof item === 'string') return item
        const loc = Array.isArray(item.loc)
          ? item.loc.filter((part) => part !== 'body').join('.')
          : ''
        return loc ? `${loc}: ${item.msg || ''}` : item.msg || JSON.stringify(item)
      })
      .filter(Boolean)
    return items.join('；') || `请求失败（${status}）`
  }
  if (typeof detail === 'object') {
    return detail.message || detail.detail || JSON.stringify(detail)
  }
  return String(detail)
}

export async function api(path, options = {}) {
  const { body, headers = {}, ...rest } = options
  const init = { ...rest, headers: { ...headers } }

  if (body !== undefined && body !== null) {
    if (body instanceof FormData) {
      init.body = body
    } else if (typeof body === 'string') {
      init.body = body
      if (!init.headers['Content-Type']) {
        init.headers['Content-Type'] = 'application/json'
      }
    } else {
      init.body = JSON.stringify(body)
      init.headers['Content-Type'] = 'application/json'
    }
  }

  const resp = await fetch(path, init)
  const rawText = await resp.text()
  let parsed = null
  if (rawText) {
    try {
      parsed = JSON.parse(rawText)
    } catch {
      parsed = null
    }
  }

  if (!resp.ok) {
    throw new ApiError(resp.status, parsed ? parsed.detail || parsed : null)
  }
  return parsed !== null ? parsed : rawText
}
