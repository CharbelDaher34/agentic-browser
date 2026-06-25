import { useEffect, useRef, useState } from 'react'
import { wsUrl } from './api.js'

// The local provider streams at viewport 1280x800; CDP input injection expects
// page CSS pixels, so we scale clicks from the displayed <img> back to that.
const VW = 1280
const VH = 800

export default function LiveView({ sessionId }) {
  const [mode, setMode] = useState(null)
  const [url, setUrl] = useState(null)
  const [frame, setFrame] = useState(null)
  const [controlling, setControlling] = useState(false)
  const [connected, setConnected] = useState(false)
  const [viewError, setViewError] = useState('')
  const wsRef = useRef(null)
  const imgRef = useRef(null)
  const boxRef = useRef(null)

  useEffect(() => {
    const ws = new WebSocket(wsUrl(`/ws/view/${sessionId}`))
    wsRef.current = ws
    ws.onopen = () => { setConnected(true); setViewError('') }
    ws.onclose = (e) => {
      setConnected(false)
      if (e.code === 4401 || e.code === 4404) setViewError('view rejected (auth/ownership)')
    }
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data)
      if (m.type === 'live_view') {
        setMode(m.mode)
        setUrl(m.url)
      } else if (m.type === 'frame') {
        setFrame(m.data)
      } else if (m.type === 'lease') {
        setControlling(!!m.granted)
        if (m.granted && boxRef.current) boxRef.current.focus()
      }
    }
    return () => ws.close()
  }, [sessionId])

  const send = (obj) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj))
  }

  const toggleControl = () => {
    if (controlling) send({ kind: 'release' })
    else send({ kind: 'take_over' })
  }

  const pagePoint = (e) => {
    const el = imgRef.current
    if (!el) return null
    const r = el.getBoundingClientRect()
    return {
      x: ((e.clientX - r.left) / r.width) * VW,
      y: ((e.clientY - r.top) / r.height) * VH,
    }
  }

  const onMouseDown = (e) => {
    if (!controlling) return
    const p = pagePoint(e)
    if (p) send({ kind: 'mouse', x: p.x, y: p.y, event: 'mousePressed' })
  }
  const onMouseUp = (e) => {
    if (!controlling) return
    const p = pagePoint(e)
    if (p) send({ kind: 'mouse', x: p.x, y: p.y, event: 'mouseReleased' })
  }
  const onKeyDown = (e) => {
    if (!controlling) return
    e.preventDefault()
    const text = e.key.length === 1 ? e.key : undefined
    send({ kind: 'key', key: e.key, text })
  }

  return (
    <section className="liveview">
      <header className="live-head">
        <span>Live browser</span>
        <span className={`conn ${connected ? 'on' : ''}`}>
          {viewError || mode || (connected ? 'waiting…' : 'connecting…')}
        </span>
        {(mode === 'screencast' || mode === 'iframe') && (
          <button
            className={controlling ? 'danger' : 'ghost'}
            onClick={toggleControl}
          >
            {controlling ? 'Release control' : 'Take over'}
          </button>
        )}
      </header>

      <div
        className={`live-box ${controlling ? 'controlling' : ''}`}
        ref={boxRef}
        tabIndex={0}
        onKeyDown={onKeyDown}
      >
        {mode === 'iframe' && url && (
          <iframe
            title="live"
            src={url}
            sandbox="allow-same-origin allow-scripts allow-forms"
            style={{ pointerEvents: controlling ? 'auto' : 'none' }}
          />
        )}
        {mode === 'screencast' &&
          (frame ? (
            <img
              ref={imgRef}
              src={`data:image/jpeg;base64,${frame}`}
              alt="live browser"
              draggable={false}
              onMouseDown={onMouseDown}
              onMouseUp={onMouseUp}
            />
          ) : (
            <div className="center muted">Waiting for first frame…</div>
          ))}
        {!mode && <div className="center muted">Connecting to browser…</div>}
      </div>
      {controlling && (
        <div className="control-hint">
          You are driving. Clicks and keystrokes go to the page. The agent is paused.
        </div>
      )}
    </section>
  )
}
