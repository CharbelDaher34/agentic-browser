import { useEffect, useReducer, useRef, useState } from 'react'
import { api, wsUrl } from '../api.js'
import { chatReducer, initialChat } from './chatReducer.js'

// Owns the chat WebSocket + reducer for the selected chat. Lifted out of ChatPanel
// so the socket is single and stays alive whether or not ChatPanel is visible
// (e.g. while the live view is maximized and a MiniChat consumes the same session).
export function useChat(chat) {
  const [state, dispatch] = useReducer(chatReducer, initialChat)
  const [connected, setConnected] = useState(false)
  const [steps, setSteps] = useState([])
  const wsRef = useRef(null)
  const chatId = chat?.chat_id

  useEffect(() => {
    if (!chatId) { setConnected(false); return }
    let alive = true
    dispatch({ type: 'hydrate', messages: [] })  // reset on chat switch
    const loadSteps = () =>
      api.chatSteps(chatId).then((r) => alive && setSteps(r.steps || [])).catch(() => {})
    api.chatMessages(chatId)
      .then((r) => alive && dispatch({ type: 'hydrate', messages: r.messages || [] }))
      .catch(() => {})
    loadSteps()

    const ws = new WebSocket(wsUrl(`/ws/chat/${chatId}`))
    wsRef.current = ws
    ws.onopen = () => alive && setConnected(true)
    ws.onclose = () => alive && setConnected(false)
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      dispatch({ type: 'event', ev: msg })
      if (msg.type === 'final' || msg.type === 'interrupted') loadSteps()
    }
    return () => {
      alive = false
      ws.close()
      wsRef.current = null
      setConnected(false)
      setSteps([])
    }
  }, [chatId])

  const send = (text) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    dispatch({ type: 'send', text })  // sending mid-run interrupts (backend start_turn)
    wsRef.current.send(JSON.stringify({ kind: 'user_message', text }))
  }
  const stop = () => wsRef.current?.send(JSON.stringify({ kind: 'interrupt' }))
  const resolveApproval = (decisions) => {
    wsRef.current?.send(JSON.stringify({ kind: 'approval', decisions }))
    dispatch({ type: 'clear_approval' })  // close the modal (data:null would set {} → stuck open)
  }

  return { state, connected, steps, send, stop, resolveApproval }
}
