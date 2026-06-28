import { useEffect, useRef, useState } from 'react'
import { wsUrl } from '../api.js'

export default function LivePanel({ sessionId, maximized, onToggleMax, running }) {
  const [mode, setMode] = useState(null)        // 'screencast' | 'iframe'
  const [iframeUrl, setIframeUrl] = useState(null)
  const [tabs, setTabs] = useState([])
  const [drivers, setDrivers] = useState({})     // tab_id -> 'agent'|'human'|'none'
  const [activeTab, setActiveTab] = useState('t0')
  const [frame, setFrame] = useState(null)        // {url, w, h} (object URL)
  const [controlled, setControlled] = useState({})// tab_id -> bool (I hold lease)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)
  const imgRef = useRef(null)
  const boxRef = useRef(null)
  const activeRef = useRef('t0')
  activeRef.current = activeTab
  const frameUrlRef = useRef(null)   // current object URL, revoked on replace/unmount

  useEffect(() => {
    let alive = true
    const ws = new WebSocket(wsUrl(`/ws/view/${sessionId}`))
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws
    ws.onopen = () => alive && setConnected(true)
    ws.onclose = () => alive && setConnected(false)
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') {
        // binary frame: [ver u8][w u16][h u16][tab_len u8][tab][jpeg]
        if (!alive) return
        const buf = ev.data
        const dv = new DataView(buf)
        const w = dv.getUint16(1), h = dv.getUint16(3), tlen = dv.getUint8(5)
        const tab = new TextDecoder().decode(new Uint8Array(buf, 6, tlen))
        if (tab !== activeRef.current) return   // not the watched tab — drop
        const blob = new Blob([new Uint8Array(buf, 6 + tlen)], { type: 'image/jpeg' })
        const url = URL.createObjectURL(blob)
        if (frameUrlRef.current) URL.revokeObjectURL(frameUrlRef.current)
        frameUrlRef.current = url
        setFrame({ url, w: w || 1280, h: h || 800 })
        return
      }
      const m = JSON.parse(ev.data)
      if (m.type === 'live_view') { setMode(m.mode); setIframeUrl(m.url || null) }
      else if (m.type === 'tabs') {
        setTabs(m.tabs || [])
        const d = {}
        for (const l of m.leases || []) d[l.tab_id] = l.driver
        setDrivers(d)
      }
      else if (m.type === 'lease') {
        setControlled((c) => ({ ...c, [m.tab_id || 't0']: !!m.granted }))
        setDrivers((d) => ({ ...d, [m.tab_id || 't0']: m.driver }))
        if (m.granted && boxRef.current) boxRef.current.focus()
      }
    }
    // periodic tab refresh so sub-agent tabs appear as they open
    const poll = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ kind: 'tabs' }))
    }, 4000)
    return () => {
      alive = false; clearInterval(poll); ws.close()
      if (frameUrlRef.current) { URL.revokeObjectURL(frameUrlRef.current); frameUrlRef.current = null }
    }
  }, [sessionId])

  const send = (obj) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj))
  }

  const isControlling = !!controlled[activeTab]

  const watchTab = (tab_id) => {
    setActiveTab(tab_id); setFrame(null)
    send({ kind: 'watch', tab_id })
  }
  const toggleControl = () => {
    if (isControlling) send({ kind: 'release', tab_id: activeTab })
    else send({ kind: 'take_over', tab_id: activeTab })
  }

  const pagePoint = (e) => {
    const el = imgRef.current
    if (!el || !frame) return null
    const r = el.getBoundingClientRect()
    // the <img> box fills the panel but object-fit:contain letterboxes the image;
    // map the click into the *displayed* image rect, then to page pixels.
    const scale = Math.min(r.width / frame.w, r.height / frame.h)
    const dispW = frame.w * scale, dispH = frame.h * scale
    const offX = (r.width - dispW) / 2, offY = (r.height - dispH) / 2
    const x = (e.clientX - r.left - offX) / scale
    const y = (e.clientY - r.top - offY) / scale
    return {
      x: Math.max(0, Math.min(x, frame.w)),
      y: Math.max(0, Math.min(y, frame.h)),
    }
  }
  const onMouseDown = (e) => { if (!isControlling) return; const p = pagePoint(e); if (p) send({ kind: 'mouse', x: p.x, y: p.y, event: 'mousePressed', tab_id: activeTab }) }
  const onMouseUp = (e) => { if (!isControlling) return; const p = pagePoint(e); if (p) send({ kind: 'mouse', x: p.x, y: p.y, event: 'mouseReleased', tab_id: activeTab }) }
  const onWheel = (e) => {
    if (!isControlling) return
    e.preventDefault()
    const p = pagePoint(e) || { x: 0, y: 0 }
    send({ kind: 'scroll', x: p.x, y: p.y, dx: e.deltaX, dy: e.deltaY, tab_id: activeTab })
  }
  const onKeyDown = (e) => {
    if (!isControlling) return
    e.preventDefault()
    const text = e.key.length === 1 ? e.key : undefined
    send({ kind: 'key', key: e.key, text, tab_id: activeTab })
  }

  return (
    <section className="panel liveview">
      <header className="panel-head">
        <span className="panel-title">Live browser</span>
        {onToggleMax && (
          <button className="btn mini ghost" onClick={onToggleMax} title={maximized ? 'Restore split view' : 'Expand to full screen'}>
            {maximized ? '⇲ Restore' : '⛶ Full screen'}
          </button>
        )}
        <span className="status-pill">
          <span className={'dot' + (connected ? ' live' : '')} />
          {connected ? (mode || 'connecting…') : 'offline'}
        </span>
      </header>

      {mode === 'screencast' && tabs.length > 0 && (
        <div className="tabs-bar">
          {tabs.map((t) => (
            <div
              key={t.tab_id}
              className={'tab-chip' + (t.tab_id === activeTab ? ' active' : '')}
              onClick={() => watchTab(t.tab_id)}
              title={t.url}
            >
              <span className={'drv ' + (drivers[t.tab_id] || 'none')} />
              <span className="t-title">{t.label || t.tab_id}{t.primary ? '' : ''}</span>
            </div>
          ))}
        </div>
      )}

      <div className="viewport">
        {mode === 'iframe' && iframeUrl && (
          <div className={'screen-wrap' + (isControlling ? ' driving' : '')}>
            <iframe title="live" src={iframeUrl}
              sandbox="allow-same-origin allow-scripts allow-forms"
              style={{ pointerEvents: isControlling ? 'auto' : 'none' }} />
          </div>
        )}
        {mode === 'screencast' && (frame ? (
          <div
            className={'screen-wrap' + (isControlling ? ' driving' : (running ? ' agent-driving' : ''))}
            ref={boxRef} tabIndex={0} onKeyDown={onKeyDown}
            style={{ outline: 'none' }}
          >
            {isControlling && <div className="driving-banner">You are driving — scroll, click & type</div>}
            {running && !isControlling && (
              <div className="agent-banner"><span className="agent-banner-pulse" /> Agent is driving</div>
            )}
            <img
              ref={imgRef}
              src={frame.url}
              alt="live browser"
              draggable={false}
              onMouseDown={onMouseDown}
              onMouseUp={onMouseUp}
              onWheel={onWheel}
              style={{ cursor: isControlling ? 'crosshair' : 'default' }}
            />
          </div>
        ) : (
          <div className="center muted">Waiting for the first frame…</div>
        ))}
        {!mode && <div className="center muted">Connecting to browser…</div>}
      </div>

      <div className="live-foot">
        <button className={'btn mini ' + (isControlling ? 'danger' : 'ghost')} onClick={toggleControl} disabled={!mode}>
          {isControlling ? 'Release control' : 'Take over'}
        </button>
        <span className="live-hint">
          {mode === 'screencast'
            ? (isControlling ? 'Scroll, click and type directly on the page.' : 'Watching the agent. Take over to drive this tab.')
            : 'Browserbase live view — take over inside the frame.'}
        </span>
      </div>
    </section>
  )
}
