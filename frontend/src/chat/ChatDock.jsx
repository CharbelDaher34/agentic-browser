import { useEffect, useRef, useState } from 'react'
import ChatPanel from './ChatPanel.jsx'

const OPEN_KEY = 'ab_chat_open'
const WIDE_KEY = 'ab_chat_wide'
const read = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v === '1' } catch { return d } }
const write = (k, v) => { try { localStorage.setItem(k, v ? '1' : '0') } catch {} }

// The conversation lives in a collapsible side panel. It sits NEXT to the live
// browser (never over it, so the page stays readable) and can be hidden to give
// the browser the full stage; a launcher bubble brings it back. The chat
// socket/state (`session`) is owned by Workspace, so hiding the panel never
// interrupts the agent — the bubble just keeps a badge of unread replies.
export default function ChatDock({ chat, session }) {
  const [open, setOpen] = useState(() => read(OPEN_KEY, true))
  const [wide, setWide] = useState(() => read(WIDE_KEY, false))
  const seenRef = useRef(session.state.messages.length)
  const [unread, setUnread] = useState(0)
  const { state } = session

  useEffect(() => { write(OPEN_KEY, open) }, [open])
  useEffect(() => { write(WIDE_KEY, wide) }, [wide])

  // count assistant replies that landed while the dock was closed
  useEffect(() => {
    const n = state.messages.length
    if (open) { seenRef.current = n; setUnread(0); return }
    let c = 0
    for (let i = seenRef.current; i < n; i++) if (state.messages[i].role === 'assistant') c++
    setUnread(c)
  }, [open, state.messages])

  // a pending approval always needs eyes on the conversation
  useEffect(() => { if (state.approval) setOpen(true) }, [state.approval])

  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === 'Escape' && !e.target.closest?.('.modal')) setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  return (
    <>
      {open && (
        <aside className={'chat-side' + (wide ? ' wide' : '')} aria-label="Chat with the agent">
          <ChatPanel
            key={chat.chat_id}
            chat={chat}
            session={session}
            wide={wide}
            onToggleWide={() => setWide((v) => !v)}
            onClose={() => setOpen(false)}
          />
        </aside>
      )}
      {!open && (
        <button
          className={'chat-launcher' + (state.running ? ' busy' : '')}
          onClick={() => setOpen(true)}
          title="Open chat"
          aria-label="Open chat"
        >
          <span className="cl-icon" aria-hidden>
            <svg viewBox="0 0 24 24" width="24" height="24"><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.4 3.3c-.6.5-1.6 0-1.6-.8V6.5z" fill="currentColor" /></svg>
          </span>
          {unread > 0 && <span className="cl-badge">{unread > 9 ? '9+' : unread}</span>}
          {state.running && <span className="cl-busy" />}
        </button>
      )}
    </>
  )
}
