import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import { ThemeToggle } from './theme.jsx'
import ChatPanel from './chat/ChatPanel.jsx'
import LivePanel from './live/LivePanel.jsx'
import { useChat } from './chat/useChat.js'
import { sumUsage } from './chat/chatReducer.js'
import { SessionUsagePill } from './chat/usage.jsx'
import ApprovalModal from './chat/ApprovalModal.jsx'
import MiniChat from './chat/MiniChat.jsx'

// Single-session UI: every user works in ONE browser session (the backend still
// supports many). We use the user's existing session, or silently create one at
// startup. Provider keys come from the server's env (no per-session key setup).
//
// Layout ("Browser hero + chat rail"): a slim top bar carries identity, the
// always-on session usage and browser status. Below it the live browser is the
// dominant canvas (left) and the conversation is a focused rail (right). The chat
// list lives in a slide-in drawer opened from the top bar.
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
  const [drawerOpen, setDrawerOpen] = useState(false)
  const creatingRef = useRef(false)               // guard against double auto-create
  const chatSession = useChat(selected)

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

  // Ensure exactly one browser session exists at start; silently create one if
  // none (provider keys come from the server's env — no setup prompt needed).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const sess = await refresh()
      if (cancelled || sess || creatingRef.current) return
      creatingRef.current = true
      try { await api.createSession({ name: 'Session' }); await refresh() }
      catch {} finally { creatingRef.current = false }
    })()
    return () => { cancelled = true }
  }, [refresh])

  const newChat = async () => {
    if (!session) return
    const title = prompt('Chat title', 'New chat')
    if (title === null) return
    const r = await api.createChat(session.session_id, title || 'New chat')
    await refresh()
    setSelected({ chat_id: r.chat_id, session_id: session.session_id, title: r.title })
    setDrawerOpen(false)
  }

  const myChats = session ? chats.filter((c) => c.session_id === session.session_id) : []
  const sessionUsage = sumUsage(chatSession.state.messages)
  const live = !!session?.live

  return (
    <div className="shell">
      <header className="topbar">
        <button className="tb-menu" onClick={() => setDrawerOpen(true)} title="Chats" aria-label="Open chats">
          <span className="tb-burger"><i /><i /><i /></span>
        </button>
        <div className="tb-brand">
          <div className="brand-mark" />
          <div className="tb-brand-text">
            <div className="brand-name">Agentic Browser</div>
            <div className="brand-sub">{selected?.title || 'drive the web with an agent'}</div>
          </div>
        </div>

        <div className="tb-spacer" />

        <SessionUsagePill total={sessionUsage} />

        <span className={'tb-status' + (live ? ' live' : '')}>
          <span className={'dot' + (live ? ' live' : '')} />
          {session ? (live ? 'browser live' : 'browser idle') : 'connecting…'}
        </span>

        <ThemeToggle />

        <div className="tb-user">
          <div className="avatar" title={user.username}>{(user.username || '?').slice(0, 2).toUpperCase()}</div>
          <button className="btn mini ghost" onClick={logout}>Sign out</button>
        </div>
      </header>

      <main className="stage">
        {selected ? (
          <div className={'workspace' + (maxLive ? ' max-live' : '')}>
            <div className="canvas">
              <LivePanel
                key={selected.session_id}
                sessionId={selected.session_id}
                maximized={maxLive}
                onToggleMax={() => setMaxLive((v) => !v)}
                running={chatSession.state.running}
              />
            </div>
            {!maxLive && (
              <div className="rail">
                <ChatPanel key={selected.chat_id} chat={selected} session={chatSession} />
              </div>
            )}
            {maxLive && <MiniChat session={chatSession} />}
            <ApprovalModal approval={chatSession.state.approval} onResolve={chatSession.resolveApproval} />
          </div>
        ) : (
          <EmptyState session={session} onNew={newChat} />
        )}
      </main>

      <ChatsDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        chats={myChats}
        session={session}
        selected={selected}
        onSelect={(c) => { setSelected({ chat_id: c.chat_id, session_id: c.session_id, title: c.title }); setDrawerOpen(false) }}
        onNew={newChat}
      />
    </div>
  )
}

// Slide-in chat switcher. Opened from the top bar; closes on scrim click or Escape.
function ChatsDrawer({ open, onClose, chats, session, selected, onSelect, onNew }) {
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="drawer-scrim" onClick={onClose}>
      <aside className="chats-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <div className="brand-mark" />
          <div className="grow">
            <div className="brand-name">Agentic Browser</div>
            <div className="brand-sub">drive the web with an agent</div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="drawer-body">
          <button className="new-chat-btn" onClick={onNew} disabled={!session}>
            <span className="nc-plus">+</span>
            <span className="grow" style={{ textAlign: 'left' }}>New chat</span>
            <span className="nc-arrow">→</span>
          </button>
          <div className="side-section-h">
            <span>Chats</span>
            {chats.length > 0 && <span className="count-badge">{chats.length}</span>}
          </div>
          {!session && <div className="side-empty">Setting up your browser session…</div>}
          {session && chats.length === 0 && (
            <div className="side-empty">No chats yet. Start one to put the agent to work.</div>
          )}
          {chats.map((c) => (
            <div
              key={c.chat_id}
              className={'chat-item' + (selected?.chat_id === c.chat_id ? ' active' : '')}
              onClick={() => onSelect(c)}
            >
              <span className="glyph">▸</span>
              <span className="grow" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {c.title || 'Untitled'}
              </span>
            </div>
          ))}
        </div>
      </aside>
    </div>
  )
}

const CAPABILITIES = [
  { ic: '🔎', t: 'Research & synthesize', d: 'Browse multiple sources and bring back one clear answer.' },
  { ic: '🧾', t: 'Fill forms & checkout', d: 'Complete multi-step flows — with approval on risky actions.' },
  { ic: '📊', t: 'Extract structured data', d: 'Pull tables, prices and listings straight off any page.' },
  { ic: '🛰️', t: 'Automate workflows', d: 'Chain tabs and sub-agents to finish the whole job.' },
]

function EmptyState({ session, onNew }) {
  return (
    <div className="hero">
      <div className="hero-glow" />
      <div className="hero-mark"><div className="brand-mark" /></div>
      <div className="hero-eyebrow">Agentic Browser</div>
      <h1 className="hero-title">Put the agent to work</h1>
      <p className="hero-sub">
        Start a chat and watch it drive a real browser — live, step by step.
        You stay in control and can take over any time.
      </p>
      <div className="hero-cards">
        {CAPABILITIES.map((c) => (
          <div className="hero-card" key={c.t}>
            <div className="hero-card-ic">{c.ic}</div>
            <div className="hero-card-t">{c.t}</div>
            <div className="hero-card-d">{c.d}</div>
          </div>
        ))}
      </div>
      <button className="btn primary hero-cta" onClick={onNew} disabled={!session}>
        {session ? '+  Start a new chat' : 'Setting up session…'}
      </button>
    </div>
  )
}
