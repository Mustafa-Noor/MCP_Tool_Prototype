import { useCallback, useEffect, useRef, useState } from "react"
import { WS_URL } from "./api"

let nextTurnId = 1

function makeTurn(query) {
  return {
    id: nextTurnId++,
    query,
    status: "running", // running | done | error
    subtasks: null,
    candidates: null,
    plan: null,
    steps: [], // [{index, skillId, params, status, output, error}]
    reply: null,
    error: null,
    elicit: null, // {id, kind, message, choices?, fields?}
  }
}

function upsertStep(steps, index, patch) {
  const existing = steps.find((s) => s.index === index)
  if (existing) {
    const next = steps.map((s) => (s.index === index ? { ...s, ...patch } : s))
    return next
  }
  return [...steps, { index, ...patch }].sort((a, b) => a.index - b.index)
}

export function usePlanner() {
  const [connected, setConnected] = useState(false)
  const [turns, setTurns] = useState([])
  const wsRef = useRef(null)
  const currentTurnId = useRef(null)
  const [busy, setBusy] = useState(false)

  const patchCurrentTurn = useCallback((patch) => {
    setTurns((prev) =>
      prev.map((t) =>
        t.id === currentTurnId.current ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t
      )
    )
  }, [])

  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => setConnected(true)
    ws.onclose = () => setConnected(false)
    ws.onerror = () => setConnected(false)

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data)

      switch (msg.type) {
        case "subtasks":
          patchCurrentTurn({ subtasks: msg.subtasks })
          break
        case "candidates":
          patchCurrentTurn({ candidates: msg.candidates })
          break
        case "plan":
          patchCurrentTurn({ plan: msg.steps })
          break
        case "step_start":
          patchCurrentTurn((t) => ({
            steps: upsertStep(t.steps, msg.index, {
              skillId: msg.skill_id,
              params: msg.params,
              status: "running",
            }),
          }))
          break
        case "step":
          patchCurrentTurn((t) => ({
            steps: upsertStep(t.steps, msg.index, {
              skillId: msg.skill_id,
              params: msg.params,
              status: msg.status,
              output: msg.output,
              error: msg.error,
            }),
          }))
          break
        case "elicit":
          patchCurrentTurn({ elicit: msg })
          break
        case "done":
          patchCurrentTurn({ status: "done", reply: msg.reply, elicit: null })
          currentTurnId.current = null
          setBusy(false)
          break
        case "error":
          patchCurrentTurn({ status: "error", error: msg.message, elicit: null })
          currentTurnId.current = null
          setBusy(false)
          break
        default:
          break
      }
    }

    return () => ws.close()
  }, [patchCurrentTurn])

  const sendQuery = useCallback((text) => {
    const trimmed = text.trim()
    if (!trimmed || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    const turn = makeTurn(trimmed)
    currentTurnId.current = turn.id
    setBusy(true)
    setTurns((prev) => [...prev, turn])
    wsRef.current.send(JSON.stringify({ type: "query", text: trimmed }))
  }, [])

  const respondElicit = useCallback(
    (id, action, value) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
      patchCurrentTurn({ elicit: null })
      wsRef.current.send(JSON.stringify({ type: "elicit_response", id, action, value }))
    },
    [patchCurrentTurn]
  )

  return { connected, turns, sendQuery, respondElicit, busy }
}
