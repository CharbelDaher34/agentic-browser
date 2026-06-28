// Usage rendering — shared by the per-message footer (ChatPanel) and the session
// total (sidebar). Usage is reported per turn by the backend `usage` StreamEvent
// and embedded into each assistant message; the session total is summed from those
// (see sumUsage in chatReducer.js). Tokens/steps/requests always render; the $ cost
// only when the backend populates cost_usd (i.e. once a price table lands).

const _n = (v) => (v || 0).toLocaleString()
// enough precision to be meaningful for sub-cent turns
const _usd = (v) => '$' + (v < 0.01 ? v.toFixed(4) : v.toFixed(2))

// Per-message usage footer, rendered below a completed assistant message.
export function UsageLine({ u }) {
  if (!u) return null
  return (
    <div className="usage-line" title={`in ${_n(u.input_tokens)} · out ${_n(u.output_tokens)} tokens`}>
      ⌚ {u.cost_usd != null && <strong>{_usd(u.cost_usd)} · </strong>}
      {_n(u.total_tokens)} tokens · {_n(u.steps)} steps · {_n(u.requests)} reqs
    </div>
  )
}

// Session total for the sidebar (summed across the loaded messages this session).
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
