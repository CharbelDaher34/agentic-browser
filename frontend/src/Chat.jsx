import { useEffect, useRef, useState, useCallback } from 'react'
import { api, wsUrl, artifactUrl } from './api.js'

export default function ChatView({ chat }) {
  const [messages, setMessages] = useState([])
  const [streaming, setStreaming] = useState('')
  const [thinking, setThinking] = useState('')
  const [activity, setActivity] = useState([]) // live tool/action/observation log
  const [steps, setSteps] = useState([])
  const [approval, setApproval] = useState(null)
  const [running, setRunning] = useState(false)
  const [connected, setConnected] = useState(false)
  const [wsError, setWsError] = useState('')
  const [draft, setDraft] = useState('')

  const wsRef = useRef(null)
  const scrollRef = useRef(null)
  const streamRef = useRef('') // accumulates tokens for the current turn

  const loadSteps = useCallback(() => {
    api.chatSteps(chat.chat_id).then((r) => setSteps(r.steps)).catch(() => {})
  }, [chat.chat_id])

  useEffect(() => {
    api.chatMessages(chat.chat_id).then((r) => setMessages(r.messages)).catch(() => {})
    loadSteps()

    const ws = new WebSocket(wsUrl(`/ws/chat/${chat.chat_id}`))
    wsRef.current = ws
    ws.onopen = () => { setConnected(true); setWsError('') }
    ws.onclose = (e) => {
      setConnected(false)
      // custom close codes 4401/4404 signal auth/ownership rejection
      if (e.code === 4401 || e.code === 4404) setWsError('Chat connection rejected (auth/ownership).')
    }
    ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data))
    return () => ws.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.chat_id])

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, streaming, activity, thinking])

  function handleEvent(msg) {
    const { type, data } = msg
    if (type === 'token') {
      streamRef.current += data.text
      setStreaming(streamRef.current)
    } else if (type === 'thinking') {
      setThinking((t) => t + data.text)
    } else if (type === 'tool_call') {
      setActivity((a) => [...a, { kind: 'tool_call', tool: data.tool, args: data.args }])
    } else if (type === 'action') {
      setActivity((a) => [...a, { kind: 'action', action: data.action, ref: data.ref }])
    } else if (type === 'observation') {
      setActivity((a) => [
        ...a,
        { kind: 'observation', idx: data.idx, url: data.url, title: data.title,
          ok: data.ok, changed: data.changed },
      ])
    } else if (type === 'approval_request') {
      setApproval(data)
    } else if (type === 'final') {
      const text = data.text || streamRef.current
      if (text) setMessages((m) => [...m, { role: 'assistant', text }])
      streamRef.current = ''
      setStreaming('')
      setThinking('')
      setRunning(false)
      loadSteps()
    } else if (type === 'error') {
      setMessages((m) => [...m, { role: 'system', text: data.msg }])
      setRunning(false)
    }
  }

  const send = () => {
    const text = draft.trim()
    if (!text || !connected || running) return
    setMessages((m) => [...m, { role: 'user', text }])
    setDraft('')
    setActivity([])
    setThinking('')
    streamRef.current = ''
    setStreaming('')
    setRunning(true)
    wsRef.current.send(JSON.stringify({ kind: 'user_message', text }))
  }

  const respondApproval = (approve) => {
    const decisions = {}
    for (const c of approval.calls) decisions[c.id] = approve ? true : false
    wsRef.current.send(JSON.stringify({ kind: 'approval', decisions }))
    setApproval(null)
  }

  return (
    <section className="chat">
      <header className="chat-head">
        <span className="chat-title">{chat.title}</span>
        <span className={`conn ${connected ? 'on' : ''}`}>
          {wsError ? wsError : connected ? 'connected' : 'connecting…'}
        </span>
      </header>

      <div className="transcript" ref={scrollRef}>
        {messages.map((m, i) => (
          <Bubble key={i} role={m.role} text={m.text} />
        ))}

        {running && (streaming || activity.length > 0 || thinking) && (
          <div className="bubble assistant live">
            {thinking && <div className="thinking">💭 {thinking}</div>}
            {activity.length > 0 && (
              <div className="activity">
                {activity.map((a, i) => (
                  <ActivityLine key={i} a={a} />
                ))}
              </div>
            )}
            {streaming && <div className="text">{streaming}</div>}
            {!streaming && !thinking && <span className="cursor">▍</span>}
          </div>
        )}

        {steps.length > 0 && <StepsTrail steps={steps} />}
      </div>

      {approval && (
        <div className="approval">
          <div className="approval-card">
            <h3>⚠ Approval required</h3>
            {approval.calls.map((c) => (
              <div key={c.id} className="approval-call">
                <code>
                  {c.tool}({JSON.stringify(c.args)})
                </code>
              </div>
            ))}
            <div className="row">
              <button className="danger" onClick={() => respondApproval(true)}>
                Approve
              </button>
              <button className="ghost" onClick={() => respondApproval(false)}>
                Deny
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="composer">
        <textarea
          rows={2}
          placeholder={running ? 'agent is working…' : 'Tell the agent what to do…'}
          value={draft}
          disabled={running}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
        />
        <button onClick={send} disabled={running || !connected || !draft.trim()}>
          Send
        </button>
      </div>
    </section>
  )
}

function Bubble({ role, text }) {
  return (
    <div className={`bubble ${role}`}>
      <div className="text">{text}</div>
    </div>
  )
}

function ActivityLine({ a }) {
  if (a.kind === 'tool_call')
    return <div className="act tool">→ {a.tool}({shortArgs(a.args)})</div>
  if (a.kind === 'action')
    return <div className="act action">▸ {a.action}{a.ref ? ` [${a.ref}]` : ''}</div>
  if (a.kind === 'observation')
    return (
      <div className="act obs">
        ↳ {a.ok ? '✓' : '✗'} {a.title || a.url}{' '}
        <span className="muted">{a.changed ? '(changed)' : '(no change)'}</span>
      </div>
    )
  return null
}

function shortArgs(args) {
  try {
    const s = typeof args === 'string' ? args : JSON.stringify(args)
    return s.length > 60 ? s.slice(0, 60) + '…' : s
  } catch {
    return ''
  }
}

function StepsTrail({ steps }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="steps-trail">
      <div className="steps-toggle" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} Recorded steps ({steps.length})
      </div>
      {open && (
        <div className="steps-grid">
          {steps.map((s) => (
            <div className="step" key={s.idx}>
              {s.screenshot_uri && <img src={artifactUrl(s.screenshot_uri)} alt={`step ${s.idx}`} />}
              <div className="step-meta">
                #{s.idx} {s.action.kind}
                {s.action.ref ? ` [${s.action.ref}]` : ''}{' '}
                {s.result.ok ? '✓' : '✗'}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
