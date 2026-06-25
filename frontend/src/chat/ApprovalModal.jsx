export default function ApprovalModal({ approval, onResolve }) {
  if (!approval) return null
  const decide = (approve) => {
    const decisions = {}
    for (const c of approval.calls || []) decisions[c.id] = !!approve
    onResolve(decisions)
  }
  return (
    <div className="modal-scrim" onClick={() => decide(false)}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="warn-ic">⚠</div>
          <div>
            <div style={{ fontWeight: 700 }}>Approval required</div>
            <div className="faint" style={{ fontSize: 12 }}>The agent wants to run a destructive action.</div>
          </div>
        </div>
        <div className="modal-body">
          {(approval.calls || []).map((c) => (
            <div className="approval-call" key={c.id}>
              <b>{c.tool}</b>({typeof c.args === 'string' ? c.args : JSON.stringify(c.args)})
            </div>
          ))}
        </div>
        <div className="modal-foot">
          <button className="btn ghost" onClick={() => decide(false)}>Deny</button>
          <button className="btn danger" onClick={() => decide(true)}>Approve &amp; run</button>
        </div>
      </div>
    </div>
  )
}
