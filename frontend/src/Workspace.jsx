import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import { ThemeToggle } from './theme.jsx'
import ChatPanel from './chat/ChatPanel.jsx'
import LivePanel from './live/LivePanel.jsx'
import Settings from './Settings.jsx'
import NewSessionModal from './NewSessionModal.jsx'
import { useChat } from './chat/useChat.js'
import ApprovalModal from './chat/ApprovalModal.jsx'
import MiniChat from './chat/MiniChat.jsx'

// Single-session UI: every user works in ONE browser session (the backend still
// supports many). We use the user's existing session, or create exactly one at
// startup — silently for a local dev, or via a keys prompt when the deploy needs
// per-user Browserbase creds.
export default function Workspace() {
  const { user, logout } = useAuth()
  const [session, setSession] = useState(null)   // the single browser session
  const [chats, setChats] = useState([])
  // restore the chat we were in across refreshes (persisted to localStorage)
  const [selected, setSelectedState] = useState(() => {
    try { return JSON.parse(localStorage.getItem('ab_selected')) } catch { return null }
  })
  const setSelected = (sel) => {
    setSelectedState(sel)
    try {
      if (sel) localStorage.setItem('ab_selected', JSON.stringify(sel))
      else localStorage.removeItem('ab_selected')
    } catch {}
  }
  const [maxLive, setMaxLive] = useState(false)   // live browser fills the workspace
  const [showSettings, setShowSettings] = useState(false)
  const [showNewSession, setShowNewSession] = useState(false)
  const [config, setConfig] = useState(null)      // browser provider + BYOK needs
  const creatingRef = useRef(false)               // guard against double auto-create
  const chatSession = useChat(selected)

  useEffect(() => { api.appConfig().then(setConfig).catch(() => {}) }, [])

  const refresh = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([api.listSessions(), api.listChats()])
      const sess = (s.sessions || [])[0] || null   // the single session
      const chatList = c.chats || []
      setSession(sess)
      setChats(chatList)
      // drop a restored selection whose chat no longer exists
      setSelectedState((sel) => {
        if (sel && !chatList.some((x) => x.chat_id === sel.chat_id)) {
          try { localStorage.removeItem('ab_selected') } catch {}
          return null
        }
        return sel
      })
      return sess
    } catch { return null }
  }, [])

  // Ensure exactly one USABLE session exists at start. Creates one when none
  // exists; on a Browserbase deploy, a session that exists but lost its keys
  // (purged when it was reaped) is treated as "needs setup" so the keys modal
  // reappears instead of letting the next chat error out.
  useEffect(() => {
    if (!config) return
    let cancelled = false
    ;(async () => {
      const sess = await refresh()
      if (cancelled) return
      if (!sess) {
        if (config.browserbase_required) {
          setShowNewSession(true)   // deploy: user must provide Browserbase creds
        } else if (!creatingRef.current) {
          creatingRef.current = true   // local/dev: just open one
          try { await api.createSession({ name: 'Session' }); await refresh() }
          catch {} finally { creatingRef.current = false }
        }
        return
      }
      // session exists — on a Browserbase deploy, make sure it still has creds
      if (config.browserbase_required) {
        try {
          const k = await api.sessionKeys(sess.session_id)
          if (!cancelled && !k.browserbase) setShowNewSession(true)
        } catch {}
      }
    })()
    return () => { cancelled = true }
  }, [config, refresh])

  const createSession = async (payload) => {
    if (session) {
      // revive the existing keyless session by adding its keys (don't make a new one)
      if (payload.browserbase) {
        await api.saveSessionBrowserbase(
          session.session_id, payload.browserbase.api_key, payload.browserbase.project_id
        )
      }
      for (const [p, key] of Object.entries(payload.keys || {})) {
        await api.saveSessionKey(session.session_id, p, key)
      }
    } else {
      await api.createSession(payload)   // throws on bad/missing creds -> shown in the modal
    }
    setShowNewSession(false)
    await refresh()
  }
  const newChat = async () => {
    if (!session) return
    const title = prompt('Chat title', 'New chat')
    if (title === null) return
    const r = await api.createChat(session.session_id, title || 'New chat')
    await refresh()
    setSelected({ chat_id: r.chat_id, session_id: session.session_id, title: r.title })
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" />
          <div className="grow">
            <div className="brand-name">Agentic Browser</div>
            <div className="brand-sub">drive the web with an agent</div>
          </div>
          <ThemeToggle />
        </div>

        <div className="sidebar-scroll">
          <div className="side-section-h">
            <span>Chats</span>
            <button className="btn mini ghost" onClick={newChat} disabled={!session}>+ New</button>
          </div>
          {!session && (config?.browserbase_required ? (
            <div style={{ padding: '8px 10px' }}>
              <div className="faint" style={{ fontSize: 13, marginBottom: 8 }}>
                Add your Browserbase keys to open a browser session.
              </div>
              <button className="btn primary mini" onClick={() => setShowNewSession(true)}>Set up session</button>
            </div>
          ) : (
            <div className="faint" style={{ padding: '8px 10px', fontSize: 13 }}>
              Setting up your browser session…
            </div>
          ))}
          {(() => {
            if (!session) return null
            const myChats = chats.filter((c) => c.session_id === session.session_id)
            if (myChats.length === 0) return (
              <div className="faint" style={{ padding: '8px 10px', fontSize: 13 }}>
                No chats yet. Start one to put the agent to work.
              </div>
            )
            return myChats.map((c) => (
              <div
                key={c.chat_id}
                className={'chat-item' + (selected?.chat_id === c.chat_id ? ' active' : '')}
                onClick={() => setSelected({ chat_id: c.chat_id, session_id: c.session_id, title: c.title })}
              >
                <span className="glyph">▸</span>
                <span className="grow" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {c.title || 'Untitled'}
                </span>
              </div>
            ))
          })()}
        </div>

        <div className="sidebar-foot">
          <div className="avatar">{(user.username || '?').slice(0, 2).toUpperCase()}</div>
          <div className="grow" style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {user.username}
          </div>
          {session && <span className={'dot' + (session.live ? ' live' : '')} title={session.live ? 'browser live' : 'browser idle'} />}
          <button className="btn mini ghost" title="Session keys" onClick={() => setShowSettings(true)}>⚙</button>
          <button className="btn mini ghost" onClick={logout}>Sign out</button>
        </div>
      </aside>

      {showSettings && <Settings sessionId={session?.session_id} enforceByok={config?.enforce_byok} onClose={() => setShowSettings(false)} />}
      {showNewSession && (
        <NewSessionModal config={config} existing={!!session} onCreate={createSession} onClose={() => setShowNewSession(false)} />
      )}

      <main className="main">
        {selected ? (
          <div className={'split' + (maxLive ? ' max-live' : '')}>
            <ChatPanel key={selected.chat_id} chat={selected} session={chatSession} />
            <LivePanel
              key={selected.session_id}
              sessionId={selected.session_id}
              maximized={maxLive}
              onToggleMax={() => setMaxLive((v) => !v)}
            />
            {maxLive && <MiniChat session={chatSession} />}
            <ApprovalModal approval={chatSession.state.approval} onResolve={chatSession.resolveApproval} />
          </div>
        ) : (
          <div className="center muted" style={{ flexDirection: 'column', gap: 8 }}>
            <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)' }}>Start a chat to begin</div>
            <div>Create a chat and put the agent to work in your browser session.</div>
          </div>
        )}
      </main>
    </div>
  )
}
