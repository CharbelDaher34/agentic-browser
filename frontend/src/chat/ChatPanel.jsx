import { useLayoutEffect, useRef, useState } from 'react'
import TurnTrail, { MarkdownText } from './TurnTrail.jsx'
import StepShot from './StepShot.jsx'
import Composer from './Composer.jsx'
import AuditView from '../AuditView.jsx'

// Presentational: the chat WS + state live in the `session` (useChat) owned by
// Workspace, so the socket survives while this panel is hidden in full-screen.
export default function ChatPanel({ chat, session }) {
  const { state, connected, steps, send, stop } = session
  const [audit, setAudit] = useState(false)
  const scrollRef = useRef(null)

  // autoscroll on new content
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [state.messages, state.live, state.subagents])

  return (
    <section className="panel">
      <header className="panel-head">
        <span className="panel-title">{chat.title || 'Chat'}</span>
        <button className="btn mini ghost" onClick={() => setAudit(true)} title="Session audit & replay">
          ⛶ Audit
        </button>
        <span className="status-pill">
          <span className={'dot' + (connected ? ' live' : '')} />
          {connected ? (state.running ? 'agent working' : 'connected') : 'connecting…'}
        </span>
      </header>

      <div className="transcript" ref={scrollRef}>
        {state.messages.map((m, i) => <MessageBubble key={i} m={m} />)}

        {state.running && (
          <div className="msg-row">
            <div className="bubble assistant live">
              <div className="role-tag">agent <span className="cursor" /></div>
              <TurnTrail items={state.live} subagents={state.subagents} live />
            </div>
          </div>
        )}

        {steps.length > 0 && <StepsTrail steps={steps} />}
      </div>

      <Composer connected={connected} running={state.running} onSend={send} onStop={stop} />
      {audit && <AuditView chat={chat} onClose={() => setAudit(false)} />}
    </section>
  )
}

function MessageBubble({ m }) {
  if (m.role === 'user') {
    return (
      <div className="msg-row user">
        <div className="bubble user"><div style={{ whiteSpace: 'pre-wrap' }}>{m.text}</div></div>
      </div>
    )
  }
  if (m.role === 'system') {
    return <div className="msg-row"><div className="bubble system">{m.text}</div></div>
  }
  // assistant — render its interleaved trail (live-committed or reload-hydrated)
  const hasItems = Array.isArray(m.items) && m.items.length > 0
  return (
    <div className="msg-row">
      <div className="bubble assistant">
        <div className="role-tag">
          agent {m.interrupted && <span style={{ color: 'var(--human)' }}>· interrupted</span>}
        </div>
        {hasItems
          ? <TurnTrail items={m.items} subagents={m.subagents} />
          : <MarkdownText text={m.text} />}
      </div>
    </div>
  )
}

function StepsTrail({ steps }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="steps">
      <div className="steps-toggle" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} Recorded steps ({steps.length})
      </div>
      {open && (
        <div className="steps-grid">
          {steps.map((s) => (
            <div className="step" key={s.idx}>
              <StepShot step={s} />
              <div className="step-meta">
                #{s.idx} {s.action?.kind}
                {s.action?.ref ? ` [${s.action.ref}]` : s.action?.x != null ? ` (${Math.round(s.action.x)},${Math.round(s.action.y)})` : ''}{' '}
                {s.result?.ok ? '✓' : '✗'}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
