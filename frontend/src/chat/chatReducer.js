// Pure reducer: maps the chat WebSocket event stream into render state.
// Crucially, on `final`/`interrupted`/`error` the live per-turn trail is COMMITTED
// into the assistant message (not discarded), so the interleaved trail stays
// visible after the turn ends and matches what reload reconstructs.

export const initialChat = {
  messages: [],     // committed: {role, text} | {role:'assistant', items[], subagents[], interrupted, usage?}
  live: [],         // current-turn main trail items
  subagents: [],    // [{id, tab, task, model, status, ok, result, last}]
  running: false,
  approval: null,   // {calls:[{id,tool,args}]}
  turnUsage: null,  // usage event for the in-flight turn (embedded into the message on commit)
}

// The session total is DERIVED from the usage embedded in committed messages —
// there is no separately-tracked accumulator. See sumUsage() (exported) which the
// sidebar uses to total whatever messages are loaded.
export function sumUsage(messages) {
  const t = { steps: 0, requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_usd: null, turns: 0 }
  for (const m of messages || []) {
    const u = m.usage
    if (!u) continue
    t.steps += u.steps || 0
    t.requests += u.requests || 0
    t.input_tokens += u.input_tokens || 0
    t.output_tokens += u.output_tokens || 0
    t.total_tokens += u.total_tokens || 0
    if (u.cost_usd != null) t.cost_usd = (t.cost_usd || 0) + u.cost_usd
    t.turns += 1
  }
  return t
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
  return { role: 'assistant', items, subagents: state.subagents, interrupted, usage: state.turnUsage }
}

function commit(state, finalText, interrupted) {
  const hasContent = state.live.length || state.subagents.length || finalText
  // the turn's usage is embedded into the committed assistant message (buildAssistant);
  // the session total is derived from messages, so nothing else to track here.
  const messages = hasContent
    ? [...state.messages, buildAssistant(state, finalText, interrupted)]
    : state.messages
  return {
    ...state, messages,
    live: [], subagents: [], running: false, approval: null, turnUsage: null,
  }
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
      live: [], subagents: [], running: true, approval: null, turnUsage: null,
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
    case 'action': {
      const tail = d.target ? ` “${d.target}”` : d.ref ? ` [${d.ref}]` : ''
      if (isSub(d)) {
        return { ...state, subagents: patchSub(state.subagents, d.tab, { last: `▸ ${d.action}${tail}` }) }
      }
      return { ...state, running: true, live: [...state.live, { kind: 'action', action: d.action, ref: d.ref, target: d.target }] }
    }
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
    case 'usage':
      // emitted just before `final`; stash it so commit() can attach it to the
      // assistant message and roll it into the session total.
      return { ...state, turnUsage: d }
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
