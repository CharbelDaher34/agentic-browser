// Usage rendering — shared by the per-message footer (ChatPanel) and the session
// total (top bar). Usage is reported per turn by the backend `usage` StreamEvent
// and embedded into each assistant message; the session total is summed from those
// (see sumUsage in chatReducer.js). Tokens/steps/requests always render; the $ cost
// only when the backend populates cost_usd (i.e. once a price table lands).

const _n = (v) => (v || 0).toLocaleString()
// enough precision to be meaningful for sub-cent turns
const _usd = (v) => '$' + (v < 0.01 ? v.toFixed(4) : v.toFixed(2))

// Per-message usage footer, rendered below every assistant message. Always shown —
// when the backend hasn't reported usage for a turn we render a quiet placeholder.
export function UsageLine({ u }) {
  if (!u) {
    return <div className="usage-line faint">⌚ usage not recorded for this turn</div>
  }
  return (
    <div className="usage-line" title={`in ${_n(u.input_tokens)} · out ${_n(u.output_tokens)} tokens`}>
      ⌚ {u.cost_usd != null && <strong>{_usd(u.cost_usd)} · </strong>}
      {_n(u.total_tokens)} tokens · {_n(u.steps)} steps · {_n(u.requests)} reqs
    </div>
  )
}

// Compact, always-on session total for the top bar (summed across loaded messages).
// Renders even at zero so the running cost of the session is always in view.
export function SessionUsagePill({ total }) {
  const t = total || {}
  return (
    <div
      className="usage-pill"
      title={`Session usage — ${_n(t.turns)} turns · in ${_n(t.input_tokens)} / out ${_n(t.output_tokens)} tokens`}
    >
      <span className="up-ic">⬡</span>
      <div className="up-main">
        {t.cost_usd != null && <span className="up-cost">{_usd(t.cost_usd)}</span>}
        <span className="up-tok">{_n(t.total_tokens || 0)} <em>tokens</em></span>
      </div>
      <div className="up-sub">{_n(t.steps || 0)} steps · {_n(t.requests || 0)} reqs · {_n(t.turns || 0)} turns</div>
    </div>
  )
}

// Session total for the sidebar (kept for compatibility; the top bar now uses
// SessionUsagePill). Summed across the loaded messages this session.
export function SessionUsageBar({ total }) {
  if (!total || !total.turns) return null
  return (
    <div className="session-usage" title={`${_n(total.turns)} turns · in ${_n(total.input_tokens)} / out ${_n(total.output_tokens)} tokens`}>
      <div className="session-usage-h">Session usage</div>
      <div className="session-usage-row">
        {total.cost_usd != null && <span className="su-cost">{_usd(total.cost_usd)}</span>}
        <span>{_n(total.total_tokens)} tokens</span>
      </div>
      <div className="session-usage-sub">
        {_n(total.steps)} steps · {_n(total.requests)} reqs · {_n(total.turns)} turns
      </div>
    </div>
  )
}
