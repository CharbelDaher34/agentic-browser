import { useEffect, useState } from 'react'

const LLM = [
  { id: 'anthropic', label: 'Anthropic (Claude)', hint: 'sk-ant-…' },
  { id: 'openai', label: 'OpenAI (GPT)', hint: 'sk-…' },
  { id: 'google', label: 'Google (Gemini)', hint: 'AIza…' },
]

// Collects a new browser session's name and its BYOK keys. Keys are stored only
// for the lifetime of this session (purged when it's reaped), so they're entered
// here at creation. Browserbase creds are required when the server runs the
// cloud-browser provider without its own keys.
export default function NewSessionModal({ config, existing, onCreate, onClose }) {
  const bbRequired = !!config?.browserbase_required
  const keyProviders = config?.key_providers || ['anthropic', 'openai', 'google']
  const [name, setName] = useState('')
  const [bbKey, setBbKey] = useState('')
  const [bbProject, setBbProject] = useState('')
  const [keys, setKeys] = useState({})
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const submit = async () => {
    if (bbRequired && !(bbKey.trim() && bbProject.trim())) {
      setErr('Browserbase API key and project ID are required.')
      return
    }
    setErr(''); setBusy(true)
    const payload = { name: name.trim() || 'Session' }
    if (bbKey.trim() && bbProject.trim()) {
      payload.browserbase = { api_key: bbKey.trim(), project_id: bbProject.trim() }
    }
    const llm = {}
    for (const p of keyProviders) if ((keys[p] || '').trim()) llm[p] = keys[p].trim()
    if (Object.keys(llm).length) payload.keys = llm
    try {
      await onCreate(payload)
    } catch (e) {
      setErr(e.message || 'Could not create the session.'); setBusy(false)
    }
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="warn-ic" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}>🌐</div>
          <div>
            <div style={{ fontWeight: 700 }}>{existing ? 'Set up browser session' : 'New browser session'}</div>
            <div className="faint" style={{ fontSize: 12 }}>
              Your keys are stored only for this session and deleted when it's reaped.
            </div>
          </div>
        </div>
        <div className="modal-body">
          {err && <div className="auth-error">{err}</div>}
          {!existing && (
            <div className="field">
              <label>Name</label>
              <input className="input" value={name} autoFocus placeholder="Session"
                onChange={(e) => setName(e.target.value)} />
            </div>
          )}

          {bbRequired && (
            <div className="key-row">
              <div className="key-row-h"><span className="key-label">Browserbase (required)</span></div>
              <input className="input" type="password" placeholder="Browserbase API key (bb_live_…)"
                value={bbKey} onChange={(e) => setBbKey(e.target.value)} style={{ marginBottom: 6 }} />
              <input className="input" placeholder="Browserbase project ID"
                value={bbProject} onChange={(e) => setBbProject(e.target.value)} />
            </div>
          )}

          <div className="faint" style={{ fontSize: 12, margin: '10px 0 4px' }}>
            {config?.enforce_byok
              ? 'Model API keys (required — bring at least one: Anthropic, OpenAI, or Gemini)'
              : "Model API keys (optional — the server's keys are used if you leave these blank)"}
          </div>
          {LLM.filter((p) => keyProviders.includes(p.id)).map((p) => (
            <div className="key-row" key={p.id}>
              <div className="key-row-h"><span className="key-label">{p.label}</span></div>
              <input className="input" type="password" placeholder={p.hint}
                value={keys[p.id] || ''}
                onChange={(e) => setKeys((d) => ({ ...d, [p.id]: e.target.value }))} />
            </div>
          ))}
        </div>
        <div className="modal-foot">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy} onClick={submit}>
            {busy ? 'Saving…' : (existing ? 'Save keys' : 'Create session')}
          </button>
        </div>
      </div>
    </div>
  )
}
