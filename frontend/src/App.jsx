import { useEffect, useState, useCallback } from 'react'
import { api, getToken, setToken, clearToken } from './api.js'
import ChatView from './Chat.jsx'
import LiveView from './LiveView.jsx'

export default function App() {
  const [user, setUser] = useState(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    if (!getToken()) {
      setReady(true)
      return
    }
    api
      .me()
      .then((r) => setUser(r.user))
      .catch(() => clearToken())
      .finally(() => setReady(true))
  }, [])

  const logout = async () => {
    try {
      await api.logout()
    } catch {}
    clearToken()
    setUser(null)
  }

  if (!ready) return <div className="center muted">Loading…</div>
  if (!user) return <Auth onAuth={setUser} />
  return <Workspace user={user} onLogout={logout} />
}

function Auth({ onAuth }) {
  const [mode, setMode] = useState('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const fn = mode === 'login' ? api.login : api.register
      const r = await fn(username.trim(), password)
      setToken(r.token)
      onAuth(r.user)
    } catch (e) {
      setErr(e.message || 'Failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="center">
      <form className="card auth" onSubmit={submit}>
        <h1>
          Agentic<span className="accent">Browser</span>
        </h1>
        <p className="muted">An AI agent that drives a real browser for you.</p>
        <input
          placeholder="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
        />
        <input
          type="password"
          placeholder="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {err && <div className="error">{err}</div>}
        <button disabled={busy} type="submit">
          {busy ? '…' : mode === 'login' ? 'Log in' : 'Create account'}
        </button>
        <div className="switch muted">
          {mode === 'login' ? "No account?" : 'Have an account?'}{' '}
          <a onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setErr('') }}>
            {mode === 'login' ? 'Register' : 'Log in'}
          </a>
        </div>
      </form>
    </div>
  )
}

function Workspace({ user, onLogout }) {
  const [sessions, setSessions] = useState([])
  const [chats, setChats] = useState([])
  const [selected, setSelected] = useState(null) // {chat_id, session_id, title}

  const refresh = useCallback(async () => {
    const [s, c] = await Promise.all([api.listSessions(), api.listChats()])
    setSessions(s.sessions)
    setChats(c.chats)
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  return (
    <div className="app">
      <Sidebar
        user={user}
        sessions={sessions}
        chats={chats}
        selected={selected}
        onSelect={setSelected}
        onRefresh={refresh}
        onLogout={onLogout}
      />
      <main className="main">
        {selected ? (
          <div className="split">
            <ChatView key={selected.chat_id} chat={selected} />
            <LiveView key={selected.session_id} sessionId={selected.session_id} />
          </div>
        ) : (
          <div className="center muted">
            <div>
              <p>Select a chat, or create a browser session to begin.</p>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}

function Sidebar({ user, sessions, chats, selected, onSelect, onRefresh, onLogout }) {
  const [name, setName] = useState('')
  const [provider, setProvider] = useState('local')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const createSession = async () => {
    setBusy(true)
    setErr('')
    try {
      await api.createSession(name.trim() || undefined, provider)
      setName('')
      await onRefresh()
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  const createChat = async (sessionId) => {
    const r = await api.createChat(sessionId, 'New chat')
    await onRefresh()
    onSelect({ chat_id: r.chat_id, session_id: sessionId, title: r.title })
  }

  return (
    <aside className="sidebar">
      <div className="brand">
        Agentic<span className="accent">Browser</span>
      </div>

      <div className="new-session card">
        <div className="row">
          <input
            placeholder="session name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="local">local</option>
            <option value="browserbase">browserbase</option>
          </select>
        </div>
        <button disabled={busy} onClick={createSession}>
          {busy ? 'starting browser…' : '+ New browser session'}
        </button>
        {err && <div className="error small">{err}</div>}
      </div>

      <div className="sessions">
        {sessions.length === 0 && <div className="muted small pad">No sessions yet.</div>}
        {sessions.map((s) => (
          <div className="session" key={s.session_id}>
            <div className="session-head">
              <span className={`dot ${s.live ? 'live' : ''}`} />
              <span className="session-name">{s.name}</span>
              <span className="tag">{s.provider}</span>
              <button className="mini" onClick={() => createChat(s.session_id)}>
                + chat
              </button>
            </div>
            <div className="chat-list">
              {chats
                .filter((c) => c.session_id === s.session_id)
                .map((c) => (
                  <div
                    key={c.chat_id}
                    className={`chat-item ${selected?.chat_id === c.chat_id ? 'active' : ''}`}
                    onClick={() =>
                      onSelect({ chat_id: c.chat_id, session_id: s.session_id, title: c.title })
                    }
                  >
                    {c.title}
                  </div>
                ))}
            </div>
          </div>
        ))}
      </div>

      <div className="user-bar">
        <span className="muted">{user.username}</span>
        <button className="mini ghost" onClick={onLogout}>
          log out
        </button>
      </div>
    </aside>
  )
}
