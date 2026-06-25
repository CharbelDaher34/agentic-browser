import { useEffect, useState } from 'react'
import { api } from './api.js'

const PROVIDERS = [
  { id: 'anthropic', label: 'Anthropic (Claude)', hint: 'sk-ant-…' },
  { id: 'openai', label: 'OpenAI (GPT)', hint: 'sk-…' },
  { id: 'google', label: 'Google (Gemini)', hint: 'AIza…' },
]

// BYOK settings: add/replace/remove a model API key per provider. Keys are
// stored encrypted server-side; we only ever learn whether one is set.
export default function Settings({ onClose }) {
  const [have, setHave] = useState({})
  const [drafts, setDrafts] = useState({})
  const [busy, setBusy] = useState('')

  const refresh = () => api.listKeys().then((r) => setHave(r.providers || {})).catch(() => {})
  useEffect(() => { refresh() }, [])
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const save = async (p) => {
    const key = (drafts[p] || '').trim()
    if (!key) return
    setBusy(p)
    try { await api.saveKey(p, key); setDrafts((d) => ({ ...d, [p]: '' })); await refresh() }
    finally { setBusy('') }
  }
  const remove = async (p) => {
    setBusy(p)
    try { await api.deleteKey(p); await refresh() } finally { setBusy('') }
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="warn-ic" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}>🔑</div>
          <div>
            <div style={{ fontWeight: 700 }}>Model API keys</div>
            <div className="faint" style={{ fontSize: 12 }}>
              Optional — your own keys are used when set, otherwise the server's.
            </div>
          </div>
        </div>
        <div className="modal-body">
          {PROVIDERS.map((p) => (
            <div className="key-row" key={p.id}>
              <div className="key-row-h">
                <span className="key-label">{p.label}</span>
                {have[p.id]
                  ? <span className="key-set">● set</span>
                  : <span className="faint" style={{ fontSize: 11 }}>using server key</span>}
              </div>
              <div className="row">
                <input
                  className="input grow" type="password" placeholder={p.hint}
                  value={drafts[p.id] || ''}
                  onChange={(e) => setDrafts((d) => ({ ...d, [p.id]: e.target.value }))}
                  onKeyDown={(e) => e.key === 'Enter' && save(p.id)}
                />
                <button className="btn primary mini" disabled={busy === p.id || !(drafts[p.id] || '').trim()}
                  onClick={() => save(p.id)}>Save</button>
                {have[p.id] && (
                  <button className="btn ghost mini" disabled={busy === p.id} onClick={() => remove(p.id)}>Remove</button>
                )}
              </div>
            </div>
          ))}
        </div>
        <div className="modal-foot">
          <button className="btn ghost" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  )
}
