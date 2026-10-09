import { useState, type FormEvent } from 'react'
import { authenticate, type AuthUser } from './auth'

export default function AuthPage({ onAuthenticated }: { onAuthenticated: (user: AuthUser) => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (mode === 'register' && password !== confirmation) { setError('两次输入的密码不一致'); return }
    setBusy(true); setError('')
    try { onAuthenticated(await authenticate(mode, email.trim(), password)) }
    catch (exc) { setError((exc as Error).message) }
    finally { setBusy(false) }
  }

  return <main className="auth-screen"><div className="auth-card"><div className="auth-brand"><span className="atoms-logo"><i /><i /><i /></span><strong>Atoms Demo</strong></div><h1>{mode === 'login' ? '欢迎回来' : '创建账号'}</h1><p>登录后继续构建、修改和管理你的项目。</p><div className="auth-tabs"><button className={mode === 'login' ? 'active' : ''} onClick={() => { setMode('login'); setError('') }}>登录</button><button className={mode === 'register' ? 'active' : ''} onClick={() => { setMode('register'); setError('') }}>注册</button></div><form onSubmit={event => void submit(event)}><label>邮箱<input type="email" autoComplete="email" required value={email} onChange={event => setEmail(event.target.value)} placeholder="you@example.com" /></label><label>密码<input type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'} minLength={8} required value={password} onChange={event => setPassword(event.target.value)} placeholder="至少 8 个字符" /></label>{mode === 'register' && <label>确认密码<input type="password" autoComplete="new-password" minLength={8} required value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>}{error && <div className="auth-error" role="alert">{error}</div>}<button className="auth-submit" type="submit" disabled={busy}>{busy ? '请稍候…' : mode === 'login' ? '登录' : '注册并开始'}</button></form></div></main>
}
