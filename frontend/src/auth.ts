export type AuthUser = { id: string; email: string }
type AuthSession = { access_token: string; expires_in: number; user: AuthUser }

let accessToken = ''
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let pendingRefresh: Promise<AuthSession> | undefined
let pendingRestore: Promise<AuthUser | null> | undefined
let lastRefreshAt = 0
const REFRESH_INTERVAL_MS = 12 * 60 * 60 * 1000
const AUTH_CHANGE_KEY = 'atoms-auth-change'

class SessionExpiredError extends Error {}

function publishAuthChange(type: 'login' | 'logout') {
  // Only a notification is shared; credentials stay in memory/HttpOnly cookies.
  try { localStorage.setItem(AUTH_CHANGE_KEY, JSON.stringify({ type, nonce: Math.random() })) } catch { /* Storage can be disabled. */ }
}

function clearSession() {
  accessToken = ''
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = undefined
}

function saveSession(session: AuthSession) {
  accessToken = session.access_token
  lastRefreshAt = Date.now()
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    void refreshSession().catch(() => { /* Only an expired session triggers logout. */ })
  }, Math.max(5000, Math.min(REFRESH_INTERVAL_MS, (session.expires_in - 60) * 1000)))
  window.dispatchEvent(new CustomEvent<AuthUser>('auth-restored', { detail: session.user }))
  return session.user
}

function refreshIfDue() {
  if (accessToken && Date.now() - lastRefreshAt >= REFRESH_INTERVAL_MS) {
    void refreshSession().catch(() => { /* A temporary network failure is not logout. */ })
  }
}

document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshIfDue() })
window.addEventListener('focus', refreshIfDue)
window.addEventListener('storage', event => {
  if (event.key !== AUTH_CHANGE_KEY || !event.newValue) return
  try {
    const { type } = JSON.parse(event.newValue) as { type: string }
    if (type === 'logout') { clearSession(); window.dispatchEvent(new Event('auth-expired')) }
    else if (type === 'login') void restoreSession().catch(() => { /* Retry on the next authenticated action. */ })
  } catch { /* Ignore malformed notifications. */ }
})

export function refreshSession(): Promise<AuthSession> {
  if (pendingRefresh) return pendingRefresh
  pendingRefresh = (async () => {
    const response = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'same-origin' })
    if (response.status === 401) {
      clearSession()
      window.dispatchEvent(new Event('auth-expired'))
      throw new SessionExpiredError('请登录后继续')
    }
    if (!response.ok) throw new Error('暂时无法确认登录状态，请重试')
    const session = await response.json() as AuthSession
    saveSession(session)
    return session
  })().finally(() => { pendingRefresh = undefined })
  return pendingRefresh
}

export function restoreSession(): Promise<AuthUser | null> {
  if (pendingRestore) return pendingRestore
  pendingRestore = (async () => {
    try {
      const status = await fetch('/api/auth/session', { credentials: 'same-origin' })
      if (!status.ok) throw new Error('暂时无法确认登录状态，请重试')
      if (!(await status.json() as { authenticated: boolean }).authenticated) { clearSession(); return null }
      return (await refreshSession()).user
    } catch (error) {
      if (error instanceof SessionExpiredError) return null
      throw error
    }
  })().finally(() => { pendingRestore = undefined })
  return pendingRestore
}

async function encryptPassword(password: string): Promise<string> {
  const passwordBytes = new TextEncoder().encode(password)
  if (passwordBytes.length < 8 || passwordBytes.length > 256) throw new Error('密码长度需为 8–256 字节')
  const response = await fetch('/api/auth/public-key')
  if (!response.ok) throw new Error('无法获取登录加密公钥')
  const { public_key } = await response.json() as { public_key: string }
  if (globalThis.crypto?.subtle) {
    const pem = public_key.replace(/-----[^-]+-----/g, '').replace(/\s/g, '')
    const keyBytes = Uint8Array.from(atob(pem), character => character.charCodeAt(0))
    const key = await crypto.subtle.importKey('spki', keyBytes, { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt'])
    const encrypted = new Uint8Array(await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, key, passwordBytes))
    return btoa(String.fromCharCode(...encrypted))
  }
  const { default: forge } = await import('node-forge')
  const publicKey = forge.pki.publicKeyFromPem(public_key)
  const encrypted = publicKey.encrypt(forge.util.encodeUtf8(password), 'RSA-OAEP', {
    md: forge.md.sha256.create(), mgf1: { md: forge.md.sha256.create() },
  })
  return forge.util.encode64(encrypted)
}

export async function authenticate(mode: 'login' | 'register', email: string, password: string): Promise<AuthUser> {
  const invite = new URLSearchParams(location.search).get('invite'); if (invite) localStorage.setItem('atoms-invite', invite)
  const encrypted_password = await encryptPassword(password)
  const response = await fetch(`/api/auth/${mode}`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, encrypted_password, ...(mode === 'register' ? { invite_code: localStorage.getItem('atoms-invite') || undefined } : {}) }) })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(body.detail || `请求失败 (${response.status})`)
  }
  const user = saveSession(await response.json() as AuthSession)
  publishAuthChange('login')
  return user
}

export async function logoutSession() {
  const response = await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
  if (!response.ok) throw new Error('退出登录失败，请重试')
  clearSession()
  publishAuthChange('logout')
  window.dispatchEvent(new Event('auth-expired'))
}

export async function authFetch(path: string, options?: RequestInit): Promise<Response> {
  if (!accessToken) await refreshSession()
  const send = () => fetch(`/api${path}`, { ...options, credentials: 'same-origin', headers: { 'Content-Type': 'application/json', ...options?.headers, Authorization: `Bearer ${accessToken}` } })
  let response = await send()
  if (response.status === 401) {
    try { await refreshSession(); response = await send() }
    catch (error) {
      if (error instanceof SessionExpiredError) throw new Error('会话已过期，请重新登录')
      throw error
    }
  }
  return response
}
