import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import { ThemeToggle } from './theme.jsx'
import ChatDock from './chat/ChatDock.jsx'
import LivePanel from './live/LivePanel.jsx'
import { useChat } from './chat/useChat.js'
import { sumUsage } from './chat/chatReducer.js'
import { SessionUsagePill } from './chat/usage.jsx'
import ApprovalModal from './chat/ApprovalModal.jsx'

// Single-session UI: every user works in ONE browser session (the backend still
// supports many). We use the user's existing session, or silently create one at
// startup. Provider keys come from the server's env (no per-session key setup).
//
// There is NO landing page: on load we open the last chat, else the most recent
// one, else create one — the user always lands directly in the workspace.
//
// Layout: a slim top bar carries identity, the always-on session usage and
// browser status. Below it the live browser owns the whole stage; the
// conversation sits in a collapsible side panel with a launcher bubble
// (bottom-right). The chat list lives in a slide-in drawer.
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
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [bootTry, setBootTry] = useState(0)       // bumped to retry a failed boot
  const bootRef = useRef(false)                   // guard against concurrent boots
  const selectedRef = useRef(selected)            // latest selection, readable from effects
  useEffect(() => { selectedRef.current = selected }, [selected])
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
      return { sess, chatList }
    } catch { return { sess: null, chatList: [] } }
  }, [])

  const createChat = useCallback(async (sessionId, title) => {
    const r = await api.createChat(sessionId, title)
    await refresh()
    setSelected({ chat_id: r.chat_id, session_id: sessionId, title: r.title })
    return r
  }, [refresh])

  // Load the session + chat list once on mount. This also validates a selection
  // restored from localStorage: refresh() drops one whose chat is gone, which
  // then hands the boot effect below a clean slate.
  useEffect(() => { refresh() }, [refresh])

  // Boot straight into the workspace — there is no landing page. Make sure a
  // browser session exists (created silently; provider keys come from the
  // server's env), then open the most recent chat, or create one.
  //
  // Runs whenever nothing is selected, so it also self-heals a stale restored
  // selection. `bootRef` only guards CONCURRENT runs — deliberately not an
  // abort-on-cleanup flag, which under StrictMode's double-mount would cancel
  // the only boot and strand the user on the loading state.
  const needsChat = !selected
  useEffect(() => {
    if (!needsChat || bootRef.current) return
    bootRef.current = true
    ;(async () => {
      try {
        let { sess, chatList } = await refresh()
        if (!sess) {
          await api.createSession({ name: 'Session' })
          ;({ sess, chatList } = await refresh())
          if (!sess) throw new Error('no session')
        }
        const mine = chatList.filter((c) => c.session_id === sess.session_id)
        if (selectedRef.current) return          // a selection landed meanwhile
        if (mine.length > 0) {
          const c = mine[0]                      // list is newest-first
          setSelected({ chat_id: c.chat_id, session_id: c.session_id, title: c.title })
        } else {
          await createChat(sess.session_id, 'New chat')
        }
      } catch {
        // server hiccup — back off and try again rather than dead-ending
        setTimeout(() => setBootTry((n) => n + 1), 3000)
      } finally { bootRef.current = false }
    })()
  }, [needsChat, bootTry, refresh, createChat])

  const newChat = async () => {
    if (!session) return
    const title = prompt('Chat title', 'New chat')
    if (title === null) return
    await createChat(session.session_id, title || 'New chat')
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
          <div className="workspace">
            <div className="canvas">
              <LivePanel
                key={selected.session_id}
                sessionId={selected.session_id}
                running={chatSession.state.running}
              />
            </div>
            <ChatDock chat={selected} session={chatSession} />
            <ApprovalModal approval={chatSession.state.approval} onResolve={chatSession.resolveApproval} />
          </div>
        ) : (
          <div className="booting">
            <span className="booting-dot" />
            {session ? 'Opening your chat…' : 'Starting your browser session…'}
          </div>
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
