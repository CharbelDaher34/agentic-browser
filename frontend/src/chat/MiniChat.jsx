import Composer from './Composer.jsx'

// Compact chat overlay shown while the live browser is full-screen, so the user
// can keep steering the agent without leaving full-screen. Uses the same session.
export default function MiniChat({ session }) {
  const { state, connected, send, stop } = session
  // last assistant message, found from the end without cloning the array (and
  // without findLast, which lacks support in older browsers/webviews)
  let lastA
  for (let i = state.messages.length - 1; i >= 0; i--) {
    if (state.messages[i].role === 'assistant') { lastA = state.messages[i]; break }
  }
  const status = state.running
    ? 'Agent working…'
    : lastA?.text
      ? lastA.text.replace(/\s+/g, ' ').slice(0, 160)
      : 'Ask the agent anything.'
  return (
    <div className="mini-chat">
      <div className="mini-chat-status">
        <span className={'dot' + (state.running ? ' live' : '')} /> {status}
      </div>
      <Composer connected={connected} running={state.running} onSend={send} onStop={stop} />
    </div>
  )
}
