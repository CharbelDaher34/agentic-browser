import { useRef, useState } from 'react'

export default function Composer({ connected, running, onSend, onStop }) {
  const [draft, setDraft] = useState('')
  const taRef = useRef(null)

  const grow = (el) => {
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 180) + 'px'
  }

  const submit = () => {
    const text = draft.trim()
    if (!text || !connected) return
    onSend(text) // sending mid-run interrupts the running turn (handled by caller)
    setDraft('')
    if (taRef.current) taRef.current.style.height = 'auto'
  }

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="composer">
      <div className="composer-box">
        <textarea
          ref={taRef}
          rows={1}
          value={draft}
          placeholder={running ? 'Steer the agent — send to interrupt and redirect…' : 'Message the agent…'}
          onChange={(e) => { setDraft(e.target.value); grow(e.target) }}
          onKeyDown={onKeyDown}
        />
        {running && (
          <button className="stop-btn" title="Stop the agent" onClick={onStop}>■</button>
        )}
        <button className="send-btn" title="Send" disabled={!draft.trim() || !connected} onClick={submit}>↑</button>
      </div>
      <div className="composer-hint">
        <span>Enter to send · Shift+Enter for newline</span>
        {!connected && <span style={{ color: 'var(--bad)' }}>disconnected</span>}
        {running && <span style={{ color: 'var(--accent)' }}>agent running — send to steer</span>}
      </div>
    </div>
  )
}
