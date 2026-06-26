import { useEffect, useState } from 'react'
import { api } from './api.js'

const PROVIDERS = [
  { id: 'anthropic', label: 'Anthropic (Claude)', hint: 'sk-ant-…' },
  { id: 'openai', label: 'OpenAI (GPT)', hint: 'sk-…' },
  { id: 'google', label: 'Google (Gemini)', hint: 'AIza…' },
]

// Per-session BYOK settings: add/replace/remove keys for ONE browser session.
// Keys are stored encrypted server-side and purged when the session is reaped;
// we only ever learn whether one is set.
export default function Settings({ sessionId, enforceByok, onClose }) {
  const [have, setHave] = useState({})
  const [bbSet, setBbSet] = useState(false)
  const [drafts, setDrafts] = useState({})
  const [bbKey, setBbKey] = useState('')
  const [bbProject, setBbProject] = useState('')
  const [busy, setBusy] = useState('')

  const refresh = () => {
    if (!sessionId) return
    api.sessionKeys(sessionId)
      .then((r) => { setHave(r.providers || {}); setBbSet(!!r.browserbase) })
      .catch(() => {})
  }
  useEffect(refresh, [sessionId])
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const save = async (p) => {
    const key = (drafts[p] || '').trim()
    if (!key) return
    setBusy(p)
    try { await api.saveSessionKey(sessionId, p, key); setDrafts((d) => ({ ...d, [p]: '' })); refresh() }
    finally { setBusy('') }
  }
  const remove = async (p) => {
    setBusy(p)
    try { await api.deleteSessionKey(sessionId, p); refresh() } finally { setBusy('') }
  }
  const saveBb = async () => {
    if (!(bbKey.trim() && bbProject.trim())) return
    setBusy('browserbase')
    try {
      await api.saveSessionBrowserbase(sessionId, bbKey.trim(), bbProject.trim())
      setBbKey(''); setBbProject(''); refresh()
    } finally { setBusy('') }
  }
  const removeBb = async () => {
    setBusy('browserbase')
    try { await api.deleteSessionBrowserbase(sessionId); refresh() } finally { setBusy('') }
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="warn-ic" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}>🔑</div>
          <div>
            <div style={{ fontWeight: 700 }}>Session keys</div>
            <div className="faint" style={{ fontSize: 12 }}>
              Keys for this session only — deleted when it's reaped.
            </div>
          </div>
        </div>
        <div className="modal-body">
          {!sessionId ? (
            <div className="faint" style={{ fontSize: 13 }}>Open a chat to manage its session's keys.</div>
          ) : (
            <>
              <div className="key-row">
                <div className="key-row-h">
                  <span className="key-label">Browserbase</span>
                  {bbSet
                    ? <span className="key-set">● set</span>
                    : <span className="faint" style={{ fontSize: 11 }}>not set</span>}
                </div>
                <input className="input" type="password" placeholder="Browserbase API key (bb_live_…)"
                  value={bbKey} onChange={(e) => setBbKey(e.target.value)} style={{ marginBottom: 6 }} />
                <div className="row">
                  <input className="input grow" placeholder="Browserbase project ID"
                    value={bbProject} onChange={(e) => setBbProject(e.target.value)} />
                  <button className="btn primary mini" disabled={busy === 'browserbase' || !(bbKey.trim() && bbProject.trim())}
                    onClick={saveBb}>Save</button>
                  {bbSet && (
                    <button className="btn ghost mini" disabled={busy === 'browserbase'} onClick={removeBb}>Remove</button>
                  )}
                </div>
              </div>

              {PROVIDERS.map((p) => (
                <div className="key-row" key={p.id}>
                  <div className="key-row-h">
                    <span className="key-label">{p.label}</span>
                    {have[p.id]
                      ? <span className="key-set">● set</span>
                      : <span className="faint" style={{ fontSize: 11 }}>{enforceByok ? 'required' : 'using server key'}</span>}
                  </div>
                  <div className="row">
                    <input className="input grow" type="password" placeholder={p.hint}
                      value={drafts[p.id] || ''}
                      onChange={(e) => setDrafts((d) => ({ ...d, [p.id]: e.target.value }))}
                      onKeyDown={(e) => e.key === 'Enter' && save(p.id)} />
                    <button className="btn primary mini" disabled={busy === p.id || !(drafts[p.id] || '').trim()}
                      onClick={() => save(p.id)}>Save</button>
                    {have[p.id] && (
                      <button className="btn ghost mini" disabled={busy === p.id} onClick={() => remove(p.id)}>Remove</button>
                    )}
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
        <div className="modal-foot">
          <button className="btn ghost" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  )
}
