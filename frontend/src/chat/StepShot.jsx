import { useState } from 'react'
import { artifactUrl } from '../api.js'

// Screenshot with the action drawn on top: a pulse ring where the agent tapped/
// typed, or an arrow for a drag. Coordinates are in screenshot-pixel space, so we
// position them as a percentage of the image's natural size (resolution-agnostic).
export default function StepShot({ step }) {
  const [dim, setDim] = useState(null)
  const a = step.action || {}
  const hasPt = a.x != null && a.y != null
  const isDrag = a.x2 != null && a.y2 != null
  const kind = a.kind || ''

  return (
    <div className="shot">
      {step.screenshot_uri ? (
        <img
          src={artifactUrl(step.screenshot_uri)}
          alt={`step ${step.idx}`}
          loading="lazy"
          onLoad={(e) => setDim({ w: e.target.naturalWidth || 1280, h: e.target.naturalHeight || 800 })}
        />
      ) : (
        <div className="shot-empty">no screenshot</div>
      )}
      {dim && hasPt && (
        isDrag ? (
          <svg className="shot-svg" viewBox={`0 0 ${dim.w} ${dim.h}`} preserveAspectRatio="none">
            <defs>
              <marker id={`ah${step.idx}`} markerWidth="8" markerHeight="8" refX="5" refY="4" orient="auto">
                <path d="M0,0 L8,4 L0,8 Z" fill="var(--accent)" />
              </marker>
            </defs>
            <line x1={a.x} y1={a.y} x2={a.x2} y2={a.y2}
              stroke="var(--accent)" strokeWidth={Math.max(dim.w, dim.h) * 0.004}
              markerEnd={`url(#ah${step.idx})`} />
          </svg>
        ) : (
          <span
            className={'shot-marker' + (kind.includes('type') ? ' type' : '')}
            style={{ left: `${(a.x / dim.w) * 100}%`, top: `${(a.y / dim.h) * 100}%` }}
            title={`${kind} (${Math.round(a.x)}, ${Math.round(a.y)})`}
          />
        )
      )}
    </div>
  )
}
