import { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { MarkdownText, shortArgs } from './chat/TurnTrail.jsx'
import StepShot from './chat/StepShot.jsx'

// Tools that perform a browser action and therefore record a screenshot step
// (everything that goes through _run_action on the backend). Used to line up each
// action tool-call with its recorded screenshot, in order.
const ACTION_TOOLS = new Set([
  'navigate', 'act', 'click_at', 'type_at', 'scroll', 'drag',
  'press_key', 'go_back', 'go_forward', 'wait',
])

// Attach each recorded step to its corresponding action tool-call, in order.
function correlate(messages, steps) {
  let si = 0
  return messages.map((m) => {
    if (m.role !== 'assistant' || !Array.isArray(m.items)) return m
    const items = m.items.map((it) => {
      if (it.kind === 'tool_call' && ACTION_TOOLS.has(it.tool) && si < steps.length) {
        return { ...it, step: steps[si++] }
      }
      return it
    })
    return { ...m, items }
  })
}

export default function AuditView({ chat, onClose }) {
  const [messages, setMessages] = useState([])
  const [steps, setSteps] = useState([])
  const [tab, setTab] = useState('flow')

  useEffect(() => {
    api.chatMessages(chat.chat_id).then((r) => setMessages(r.messages || [])).catch(() => {})
    api.chatSteps(chat.chat_id).then((r) => setSteps(r.steps || [])).catch(() => {})
  }, [chat.chat_id])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const flow = useMemo(() => correlate(messages, steps), [messages, steps])

  return (
    <div className="audit">
      <header className="audit-head">
        <div>
          <div className="audit-title">Session audit</div>
          <div className="faint" style={{ fontSize: 12 }}>{chat.title} · {steps.length} steps · {messages.length} messages</div>
        </div>
        <div className="audit-tabs">
          <button className={'btn mini ' + (tab === 'flow' ? 'primary' : 'ghost')} onClick={() => setTab('flow')}>Reasoning &amp; steps</button>
          <button className={'btn mini ' + (tab === 'replay' ? 'primary' : 'ghost')} onClick={() => setTab('replay')}>Screenshot replay</button>
        </div>
        <button className="btn ghost" onClick={onClose}>✕ Close</button>
      </header>

      <div className="audit-body">
        {tab === 'flow' ? (
          <div className="audit-flow">
            {flow.length === 0 && <div className="center muted">No conversation yet.</div>}
            {flow.map((m, i) => <FlowMessage key={i} m={m} />)}
          </div>
        ) : (
          <div className="audit-steps">
            {steps.length === 0 && <div className="center muted">No recorded steps for this chat.</div>}
            {steps.map((s) => <AuditStep key={s.idx} step={s} />)}
          </div>
        )}
      </div>
    </div>
  )
}

function FlowItem({ it }) {
  if (it.kind === 'thinking') return <div className="flow-think">💭 {it.text}</div>
  if (it.kind === 'text') return <div className="flow-text"><MarkdownText text={it.text} /></div>
  if (it.kind === 'tool_call') {
    return (
      <div className="flow-tool">
        <div className="flow-tool-h mono">→ <b>{it.tool}</b>(<span className="faint">{shortArgs(it.args, 110)}</span>)</div>
        {it.step && (
          <div className="flow-tool-shot">
            <StepShot step={it.step} />
            <div className="flow-tool-cap mono">
              step #{it.step.idx} · {it.step.result?.ok ? '✓' : '✗'} {it.step.result?.observation?.title || it.step.result?.observation?.url || ''}
            </div>
          </div>
        )}
        {it.result?.text && <div className="flow-tool-res mono">{String(it.result.text).slice(0, 280)}</div>}
      </div>
    )
  }
  return null
}

function FlowMessage({ m }) {
  if (m.role === 'user') {
    return (
      <div className="flow-msg">
        <div className="flow-role user">User</div>
        <div className="flow-body" style={{ whiteSpace: 'pre-wrap' }}>{m.text}</div>
      </div>
    )
  }
  if (m.role === 'system') {
    return (
      <div className="flow-msg">
        <div className="flow-role" style={{ color: 'var(--bad)' }}>System</div>
        <div className="flow-body" style={{ color: 'var(--bad)' }}>{m.text}</div>
      </div>
    )
  }
  return (
    <div className="flow-msg">
      <div className="flow-role agent">Agent</div>
      <div className="flow-body">
        {Array.isArray(m.items) && m.items.length
          ? m.items.map((it, i) => <FlowItem key={i} it={it} />)
          : <MarkdownText text={m.text} />}
      </div>
    </div>
  )
}

function AuditStep({ step }) {
  const a = step.action || {}
  const r = step.result || {}
  const obs = r.observation || {}
  const coords = a.x != null ? `(${Math.round(a.x)}, ${Math.round(a.y)})${a.x2 != null ? ` → (${Math.round(a.x2)}, ${Math.round(a.y2)})` : ''}` : ''
  return (
    <div className="audit-step">
      <div className="audit-step-shot"><StepShot step={step} /></div>
      <div className="audit-step-info">
        <div className="audit-step-h">
          <span className="audit-idx">#{step.idx}</span>
          <span className="audit-action mono">{a.kind}</span>
          {a.risk && a.risk !== 'safe' && <span className={'audit-risk ' + a.risk}>{a.risk}</span>}
          <span className={'audit-ok ' + (r.ok ? 'ok' : 'bad')}>{r.ok ? '✓ ok' : '✗ failed'}</span>
          {r.changed ? <span className="faint" style={{ fontSize: 11 }}>page changed</span> : <span className="faint" style={{ fontSize: 11 }}>no change</span>}
        </div>
        <dl className="audit-kv">
          {a.ref && <><dt>ref</dt><dd className="mono">{a.ref}</dd></>}
          {coords && <><dt>coords</dt><dd className="mono">{coords}</dd></>}
          {a.text && <><dt>text</dt><dd>“{a.text}”</dd></>}
          {a.url && <><dt>url</dt><dd className="mono">{a.url}</dd></>}
          {a.keys && <><dt>keys</dt><dd className="mono">{a.keys}</dd></>}
          {a.direction && <><dt>scroll</dt><dd>{a.direction}</dd></>}
          {(obs.url || r.error) && <><dt>result</dt><dd className="mono">{r.error || obs.title || obs.url}</dd></>}
        </dl>
      </div>
    </div>
  )
}
