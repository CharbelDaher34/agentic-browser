// Thin REST client. Token lives in localStorage; all calls are same-origin
// relative URLs (Vite proxies them to the backend in dev).

const TOKEN_KEY = 'ab_token'

export const getToken = () => localStorage.getItem(TOKEN_KEY)
export const setToken = (t) => localStorage.setItem(TOKEN_KEY, t)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)

async function req(method, path, body) {
  const headers = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  const res = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail || detail
    } catch {}
    const err = new Error(detail)
    err.status = res.status
    throw err
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  authConfig: () => req('GET', '/api/auth/config'),
  register: (username, password) =>
    req('POST', '/api/auth/register', { username, password }),
  login: (username, password) =>
    req('POST', '/api/auth/login', { username, password }),
  me: () => req('GET', '/api/auth/me'),
  logout: () => req('POST', '/api/auth/logout'),

  // what the UI must ask for (browser provider + whether BYOK creds are required)
  appConfig: () => req('GET', '/api/config'),

  listSessions: () => req('GET', '/api/sessions'),
  // opts: { name, provider?, browserbase?: {api_key, project_id}, keys?: {provider: key} }
  createSession: (opts) => req('POST', '/api/sessions', opts),
  sessionDetail: (sid) => req('GET', `/api/sessions/${sid}`),

  listChats: (sessionId) =>
    req('GET', sessionId ? `/api/chats?session_id=${sessionId}` : '/api/chats'),
  createChat: (session_id, title) =>
    req('POST', '/api/chats', { session_id, title }),
  chatMessages: (cid) => req('GET', `/api/chats/${cid}/messages`),
  chatSteps: (cid) => req('GET', `/api/chats/${cid}/steps`),

  // BYOK keys — scoped to a browser session (purged when the session is reaped)
  sessionKeys: (sid) => req('GET', `/api/sessions/${sid}/keys`),
  saveSessionKey: (sid, provider, key) =>
    req('PUT', `/api/sessions/${sid}/keys/${provider}`, { key }),
  deleteSessionKey: (sid, provider) =>
    req('DELETE', `/api/sessions/${sid}/keys/${provider}`),
  saveSessionBrowserbase: (sid, api_key, project_id) =>
    req('PUT', `/api/sessions/${sid}/browserbase`, { api_key, project_id }),
  deleteSessionBrowserbase: (sid) =>
    req('DELETE', `/api/sessions/${sid}/browserbase`),
}

// Build a same-origin WebSocket URL (works through the Vite proxy and in prod).
export function wsUrl(path) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const token = getToken()
  const sep = path.includes('?') ? '&' : '?'
  return `${proto}://${location.host}${path}${sep}token=${encodeURIComponent(token || '')}`
}

// Screenshots are served by an auth-checked route; <img> can't send headers,
// so the token rides as a query param.
export function artifactUrl(uri) {
  if (!uri) return uri
  const token = getToken()
  const sep = uri.includes('?') ? '&' : '?'
  return `${uri}${sep}token=${encodeURIComponent(token || '')}`
}
