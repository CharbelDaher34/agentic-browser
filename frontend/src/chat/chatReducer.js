// Pure reducer: maps the chat WebSocket event stream into render state.
// Crucially, on `final`/`interrupted`/`error` the live per-turn trail is COMMITTED
// into the assistant message (not discarded), so the interleaved trail stays
// visible after the turn ends and matches what reload reconstructs.

export const initialChat = {
  messages: [],     // committed: {role, text} | {role:'assistant', items[], subagents[], interrupted}
  live: [],         // current-turn main trail items
  subagents: [],    // [{id, tab, task, model, status, ok, result, last}]
  running: false,
  approval: null,   // {calls:[{id,tool,args}]}
}

const isSub = (d) => typeof d?.agent === 'string' && d.agent.startsWith('sub:')

function appendText(live, kind, text) {
  const last = live[live.length - 1]
  if (last && last.kind === kind) {
    return [...live.slice(0, -1), { ...last, text: last.text + text }]
  }
  return [...live, { kind, text }]
}

function patchSub(subs, tab, patch) {
  return subs.map((s) => (s.tab === tab ? { ...s, ...patch } : s))
}

function buildAssistant(state, finalText, interrupted) {
  // finalText (the agent's authoritative output) is the complete answer. The
  // streamed tokens reconstruct the same text but can be incomplete (a missing
  // first chunk, etc.), so we never *append* finalText next to a streamed copy —
  // we REPLACE the last streamed text item with it (or append if none existed).
  let items = [...state.live]
  if (finalText && finalText.trim()) {
    let idx = -1
    for (let i = items.length - 1; i >= 0; i--) {
      if (items[i].kind === 'text') { idx = i; break }
    }
    if (idx >= 0) items[idx] = { ...items[idx], text: finalText }
    else items = [...items, { kind: 'text', text: finalText }]
  }
  return { role: 'assistant', items, subagents: state.subagents, interrupted }
}

function commit(state, finalText, interrupted) {
  const hasContent = state.live.length || state.subagents.length || finalText
  const messages = hasContent
    ? [...state.messages, buildAssistant(state, finalText, interrupted)]
    : state.messages
  return { ...state, messages, live: [], subagents: [], running: false, approval: null }
}

export function chatReducer(state, action) {
  if (action.type === 'hydrate') {
    return { ...initialChat, messages: action.messages || [] }
  }
  if (action.type === 'send') {
    // sending mid-run interrupts: commit the partial trail (if any) as an
    // interrupted assistant message BEFORE appending the new user message.
    let base = state
    if (state.running && (state.live.length || state.subagents.length)) {
      base = commit(state, '', true)
    }
    return {
      ...base,
      messages: [...base.messages, { role: 'user', text: action.text }],
      live: [], subagents: [], running: true, approval: null,
    }
  }
  if (action.type === 'connecting') {
    return state
  }
  if (action.type === 'clear_approval') {
    return { ...state, approval: null }
  }
  if (action.type !== 'event') return state

  const { type, data } = action.ev
  const d = data || {}
  switch (type) {
    case 'token':
      return { ...state, running: true, live: appendText(state.live, 'text', d.text || '') }
    case 'thinking':
      return { ...state, running: true, live: appendText(state.live, 'thinking', d.text || '') }
    case 'tool_call':
      return { ...state, running: true, live: [...state.live, { kind: 'tool_call', tool: d.tool, args: d.args }] }
    case 'action':
      if (isSub(d)) {
        return { ...state, subagents: patchSub(state.subagents, d.tab, { last: `▸ ${d.action}${d.ref ? ` [${d.ref}]` : ''}` }) }
      }
      return { ...state, running: true, live: [...state.live, { kind: 'action', action: d.action, ref: d.ref }] }
    case 'observation':
      if (isSub(d)) {
        return { ...state, subagents: patchSub(state.subagents, d.tab, { last: `↳ ${d.ok ? '✓' : '✗'} ${d.title || d.url || ''}` }) }
      }
      return {
        ...state,
        live: [...state.live, { kind: 'observation', idx: d.idx, url: d.url, title: d.title, ok: d.ok, changed: d.changed }],
      }
    case 'subagent_start':
      return { ...state, subagents: [...state.subagents, { id: d.id, tab: d.tab, task: d.task, model: d.model, status: 'running', last: '' }] }
    case 'subagent_end':
      return { ...state, subagents: state.subagents.map((s) => (s.id === d.id ? { ...s, status: 'done', ok: d.ok, result: d.result } : s)) }
    case 'approval_request':
      return { ...state, approval: d }
    case 'final':
      return commit(state, d.text || '', false)
    case 'interrupted':
      // Stop button (no new user message). If nothing was produced, just stop.
      if (!state.live.length && !state.subagents.length) return { ...state, running: false }
      return commit(state, '', true)
    case 'error': {
      const c = commit(state, '', false)
      return { ...c, messages: [...c.messages, { role: 'system', text: d.msg }] }
    }
    default:
      return state
  }
}
