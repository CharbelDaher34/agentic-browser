import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import { ThemeToggle } from './theme.jsx'
import ChatPanel from './chat/ChatPanel.jsx'
import LivePanel from './live/LivePanel.jsx'
import Settings from './Settings.jsx'
import { useChat } from './chat/useChat.js'
import ApprovalModal from './chat/ApprovalModal.jsx'
import MiniChat from './chat/MiniChat.jsx'

export default function Workspace() {
  const { user, logout } = useAuth()
  const [sessions, setSessions] = useState([])
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
  // single chat session (WS + reducer) shared by ChatPanel + the full-screen MiniChat
  const chatSession = useChat(selected)

  const refresh = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([api.listSessions(), api.listChats()])
      const chatList = c.chats || []
      setSessions(s.sessions || [])
      setChats(chatList)
      // drop a restored selection whose chat no longer exists
      setSelectedState((sel) => {
        if (sel && !chatList.some((x) => x.chat_id === sel.chat_id)) {
          try { localStorage.removeItem('ab_selected') } catch {}
          return null
        }
        return sel
      })
    } catch {}
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const newSession = async () => {
    const name = prompt('Name this browser session', 'Session ' + (sessions.length + 1))
    if (name === null) return
    await api.createSession(name || 'Session', undefined)
    refresh()
  }
  const newChat = async (session_id) => {
    const title = prompt('Chat title', 'New chat')
    if (title === null) return
    const r = await api.createChat(session_id, title || 'New chat')
    await refresh()
    setSelected({ chat_id: r.chat_id, session_id, title: r.title })
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
            <span>Browser sessions</span>
            <button className="btn mini ghost" onClick={newSession}>+ New</button>
          </div>
          {sessions.length === 0 && (
            <div className="faint" style={{ padding: '8px 10px', fontSize: 13 }}>
              No sessions yet. Create one to open a browser.
            </div>
          )}
          {sessions.map((s) => {
            const sessionChats = chats.filter((c) => c.session_id === s.session_id)
            return (
              <div className="session" key={s.session_id}>
                <div className="session-head" onClick={() => newChat(s.session_id)}>
                  <span className={'dot' + (s.live ? ' live' : '')} />
                  <span className="session-name">{s.name || 'Session'}</span>
                  <span className="session-badge mono">{s.provider}</span>
                </div>
                <div className="chat-list">
                  {sessionChats.map((c) => (
                    <div
                      key={c.chat_id}
                      className={'chat-item' + (selected?.chat_id === c.chat_id ? ' active' : '')}
                      onClick={() => setSelected({ chat_id: c.chat_id, session_id: s.session_id, title: c.title })}
                    >
                      <span className="glyph">▸</span>
                      <span className="grow" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {c.title || 'Untitled'}
                      </span>
                    </div>
                  ))}
                  <div className="chat-item" onClick={() => newChat(s.session_id)} style={{ color: 'var(--text-faint)' }}>
                    <span className="glyph">+</span> New chat
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        <div className="sidebar-foot">
          <div className="avatar">{(user.username || '?').slice(0, 2).toUpperCase()}</div>
          <div className="grow" style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {user.username}
          </div>
          <button className="btn mini ghost" title="Model API keys" onClick={() => setShowSettings(true)}>⚙</button>
          <button className="btn mini ghost" onClick={logout}>Sign out</button>
        </div>
      </aside>

      {showSettings && <Settings onClose={() => setShowSettings(false)} />}

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
            {/* mini composer floats over the live view when it's full-screen */}
            {maxLive && <MiniChat session={chatSession} />}
            {/* approval gate lives here so it shows even when ChatPanel is hidden */}
            <ApprovalModal approval={chatSession.state.approval} onResolve={chatSession.resolveApproval} />
          </div>
        ) : (
          <div className="center muted" style={{ flexDirection: 'column', gap: 8 }}>
            <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)' }}>Pick a chat to begin</div>
            <div>Create a browser session, then start a chat to put the agent to work.</div>
          </div>
        )}
      </main>
    </div>
  )
}
