import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const LANE_VARS = ['var(--lane-1)', 'var(--lane-2)', 'var(--lane-3)', 'var(--lane-4)']

export function MarkdownText({ text }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{ a: (p) => <a {...p} target="_blank" rel="noreferrer" /> }}
      >
        {text || ''}
      </ReactMarkdown>
    </div>
  )
}

// Collapsed-by-default disclosure. Used for thinking and tool boxes so they don't
// dominate the chat; the header (+ a short preview) is always visible, click to open.
function Collapsible({ icon, label, preview, className = '', children }) {
  const [open, setOpen] = useState(false)
  return (
    <div className={'collapse ' + className + (open ? ' open' : '')}>
      <button className="collapse-h" onClick={() => setOpen((o) => !o)}>
        <span className="collapse-caret">{open ? '▾' : '▸'}</span>
        {icon && <span className="collapse-ic">{icon}</span>}
        <span className="collapse-label">{label}</span>
        {!open && preview && <span className="collapse-preview">{preview}</span>}
      </button>
      {open && <div className="collapse-body">{children}</div>}
    </div>
  )
}

// Render a tool-call's args as a compact `k=v, …` string, truncated to `max` chars.
export function shortArgs(args, max = 90) {
  if (args == null) return ''
  let obj = args
  if (typeof args === 'string') {
    try { obj = JSON.parse(args) } catch { return args.slice(0, max) }
  }
  if (typeof obj !== 'object') return String(obj).slice(0, max)
  const s = Object.entries(obj).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')
  return s.length > max ? s.slice(0, max - 2) + '…' : s
}

const clip = (t, n) => (t && t.length > n ? t.slice(0, n) + '…' : t || '')

// A short, human-readable description of a recorded action (audit + live trail), e.g.
// Clicked “Place Order” / Typed “hello” into “Search”. `a` is an Action.to_json() dict.
export function describeAction(a) {
  if (!a) return ''
  const xy = (x, y) => `(${Math.round(x)}, ${Math.round(y)})`
  const tgt = a.target ? `“${a.target}”` : (a.ref ? `[${a.ref}]` : '')
  switch (a.kind) {
    case 'navigate':  return `Navigated to ${a.url || ''}`.trim()
    case 'click':
    case 'click_at':  return tgt ? `Clicked ${tgt}` : `Clicked at ${xy(a.x, a.y)}`
    case 'type':
    case 'type_at':   return `Typed “${a.text ?? ''}”${tgt ? ` into ${tgt}` : ''}${a.submit ? ' then Enter' : ''}`
    case 'select':    return `Selected “${a.text ?? ''}”${tgt ? ` in ${tgt}` : ''}`
    case 'scroll':
    case 'scroll_at': return `Scrolled ${a.direction || 'down'}`
    case 'key':       return `Pressed ${a.keys || ''}`
    case 'drag':      return `Dragged ${xy(a.x, a.y)} → ${xy(a.x2, a.y2)}`
    case 'back':      return 'Went back'
    case 'forward':   return 'Went forward'
    case 'wait':      return `Waited ${a.seconds ?? ''}s`
    case 'extract':   return 'Read the page'
    default:          return a.kind || ''
  }
}

// One trail item rendered WITHOUT its own collapsible (used inside a work group).
function RawItem({ it }) {
  switch (it.kind) {
    case 'thinking':
      return <div className="think-text">💭 {it.text}</div>
    case 'tool_call':
      return (
        <div className="raw-tool">
          <div className="raw-tool-h mono">→ <b>{it.tool}</b>(<span className="args">{shortArgs(it.args)}</span>)</div>
          {it.result?.text && (
            <div className="tool-result-box mono">
              {it.result.ok === false ? '✗ ' : ''}{clip(String(it.result.text), 400)}
            </div>
          )}
        </div>
      )
    case 'action':
      return <div className="trail-row action"><span className="icn">▸</span><span className="body">{it.action}{it.target ? ` “${it.target}”` : it.ref ? ` [${it.ref}]` : ''}</span></div>
    case 'observation':
      return (
        <div className="trail-row obs">
          <span className="icn">↳</span>
          <span className="body">
            <span className={it.ok ? 'ok' : 'bad'}>{it.ok ? '✓' : '✗'}</span>{' '}
            {it.title || it.url || ''}{' '}
            <span className="args">{it.changed ? '(changed)' : '(no change)'}</span>
          </span>
        </div>
      )
    default:
      return null
  }
}

// A contiguous run of non-text items (thinking + tool calls + actions/obs) shown
// as ONE collapsible "work" box so it doesn't dominate the chat.
function WorkGroup({ items }) {
  const steps = items.filter((i) => i.kind === 'tool_call' || i.kind === 'action').length
  const firstThink = items.find((i) => i.kind === 'thinking')
  const firstTool = items.find((i) => i.kind === 'tool_call')
  const label = steps ? `Worked — ${steps} step${steps === 1 ? '' : 's'}` : 'Thinking'
  const preview = firstThink ? clip(firstThink.text, 60) : firstTool ? firstTool.tool : ''
  return (
    <Collapsible icon={steps ? '🔧' : '💭'} label={label} className="c-work" preview={preview}>
      <div className="work-items">
        {items.map((it, i) => <RawItem it={it} key={i} />)}
      </div>
    </Collapsible>
  )
}

// Split items into text vs. contiguous work-groups, preserving order.
function segment(items) {
  const out = []
  let group = []
  const flush = () => { if (group.length) { out.push({ work: group }); group = [] } }
  for (const it of items || []) {
    if (it.kind === 'text') { flush(); out.push({ text: it }) }
    else group.push(it)
  }
  flush()
  return out
}

function SubAgentLanes({ subagents }) {
  if (!subagents?.length) return null
  return (
    <div className="lanes">
      {subagents.map((s, i) => (
        <div className="lane" key={s.id || i} style={{ ['--lane']: LANE_VARS[i % LANE_VARS.length] }}>
          <div className="lane-head">
            <span className="lane-tab">{s.tab}</span>
            <span className="lane-task">{s.task}</span>
            <span className="lane-model">{s.model}</span>
            <span className={'lane-status ' + s.status}>
              {s.status === 'running'
                ? <><span className="spin">◜</span> running</>
                : <span style={{ color: s.ok === false ? 'var(--bad)' : 'var(--ok)' }}>{s.ok === false ? '✗ failed' : '✓ done'}</span>}
            </span>
          </div>
          {s.status === 'running' && s.last && <div className="lane-body">{s.last}</div>}
          {s.result && <div className="lane-result">{s.result}</div>}
        </div>
      ))}
    </div>
  )
}

export default function TurnTrail({ items, subagents, live }) {
  const segs = segment(items)
  return (
    <div className="trail">
      {segs.map((s, i) =>
        s.text
          ? <MarkdownText key={i} text={s.text.text} />
          : <WorkGroup key={i} items={s.work} />
      )}
      <SubAgentLanes subagents={subagents} />
      {live && (segs.length === 0 && !subagents?.length) && <span className="cursor" />}
    </div>
  )
}
