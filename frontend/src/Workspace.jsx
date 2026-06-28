import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import { ThemeToggle } from './theme.jsx'
import ChatPanel from './chat/ChatPanel.jsx'
import LivePanel from './live/LivePanel.jsx'
import { useChat } from './chat/useChat.js'
import { sumUsage } from './chat/chatReducer.js'
import { SessionUsageBar } from './chat/usage.jsx'
import ApprovalModal from './chat/ApprovalModal.jsx'
import MiniChat from './chat/MiniChat.jsx'

// Single-session UI: every user works in ONE browser session (the backend still
// supports many). We use the user's existing session, or silently create one at
// startup. Provider keys come from the server's env (no per-session key setup).
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
          {!session && (
            <div className="faint" style={{ padding: '8px 10px', fontSize: 13 }}>
              Setting up your browser session…
            </div>
          )}
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

        {selected && <SessionUsageBar total={sumUsage(chatSession.state.messages)} />}

        <div className="sidebar-foot">
          <div className="avatar">{(user.username || '?').slice(0, 2).toUpperCase()}</div>
          <div className="grow" style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {user.username}
          </div>
          {session && <span className={'dot' + (session.live ? ' live' : '')} title={session.live ? 'browser live' : 'browser idle'} />}
          <button className="btn mini ghost" onClick={logout}>Sign out</button>
        </div>
      </aside>

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
