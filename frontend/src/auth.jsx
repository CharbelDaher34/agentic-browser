import { createContext, useContext, useEffect, useState } from 'react'
import { api, clearToken, getToken, setToken } from './api.js'
import { ThemeToggle } from './theme.jsx'

const AuthCtx = createContext(null)
export const useAuth = () => useContext(AuthCtx)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    if (!getToken()) { setReady(true); return }
    let cancelled = false
    // Only clear the token on a real auth failure (401). A network error means the
    // backend is unreachable (e.g. still booting in dev) — retry a few times and,
    // if it never answers, keep the token so a reload doesn't log the user out.
    const attempt = async (tries) => {
      try {
        const r = await api.me()
        if (!cancelled) { setUser(r.user); setReady(true) }
      } catch (e) {
        if (cancelled) return
        if (e.status === 401) { clearToken(); setReady(true); return }
        if (tries > 0) { setTimeout(() => attempt(tries - 1), 800); return }
        setReady(true)  // give up gracefully without clearing the token
      }
    }
    attempt(5)
    return () => { cancelled = true }
  }, [])

  const logout = async () => {
    try { await api.logout() } catch {}
    clearToken()
    try { localStorage.removeItem('ab_selected') } catch {}
    setUser(null)
  }

  return (
    <AuthCtx.Provider value={{ user, setUser, ready, logout }}>{children}</AuthCtx.Provider>
  )
}

export function AuthScreen() {
  const { setUser } = useAuth()
  const [mode, setMode] = useState('login')
  const [canRegister, setCanRegister] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  // Hide the sign-up form unless the backend allows registration (a locked-down
  // deploy is login-only). Defaults to false so signup never flashes.
  useEffect(() => {
    api.authConfig()
      .then((c) => setCanRegister(!!c.allow_registration))
      .catch(() => setCanRegister(false))
  }, [])

  const submit = async (e) => {
    e.preventDefault()
    setErr(''); setBusy(true)
    try {
      const fn = mode === 'login' ? api.login : api.register
      const r = await fn(username.trim(), password)
      setToken(r.token)
      setUser(r.user)
    } catch (e2) {
      setErr(e2.message || 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-wrap">
      <div className="auth-stage">
        <aside className="auth-showcase">
          <div className="auth-brand">
            <div className="brand-mark" />
            <div>
              <div className="brand-name">Agentic Browser</div>
              <div className="brand-sub">drive the web with an agent</div>
            </div>
          </div>
          <h1 className="showcase-title">The web,<br />on autopilot.</h1>
          <p className="showcase-sub">
            Give an AI agent a goal and watch it navigate, click and type through a
            real browser — with you in the loop for anything that matters.
          </p>
          <ul className="showcase-list">
            <li><span className="sc-dot" /> Live screencast of every step</li>
            <li><span className="sc-dot" /> Take over the browser any time</li>
            <li><span className="sc-dot" /> Approval gates on destructive actions</li>
            <li><span className="sc-dot" /> Full audit &amp; replay of each session</li>
          </ul>
        </aside>

        <form className="auth-card" onSubmit={submit}>
          <div className="auth-card-top">
            <div>
              <div className="auth-title">{mode === 'login' ? 'Welcome back' : 'Create your account'}</div>
              <div className="auth-sub">
                {mode === 'login' ? 'Sign in to your sessions and chats.' : 'Start steering browsers with AI.'}
              </div>
            </div>
            <ThemeToggle />
          </div>
          {err && <div className="auth-error">{err}</div>}
          <div className="field">
            <label>Username</label>
            <input className="input" value={username} autoFocus
              onChange={(e) => setUsername(e.target.value)} placeholder="ada" />
          </div>
          <div className="field">
            <label>Password</label>
            <input className="input" type="password" value={password}
              onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
          </div>
          <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 8 }}
            disabled={busy || !username || !password}>
            {busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}
          </button>
          {canRegister && (
            <div className="auth-switch">
              {mode === 'login' ? (
                <>New here? <a onClick={() => { setMode('register'); setErr('') }}>Create an account</a></>
              ) : (
                <>Have an account? <a onClick={() => { setMode('login'); setErr('') }}>Sign in</a></>
              )}
            </div>
          )}
        </form>
      </div>
    </div>
  )
}
