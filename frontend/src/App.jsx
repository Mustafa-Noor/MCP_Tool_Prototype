import { useEffect, useRef, useState } from "react"
import Sidebar from "./components/Sidebar"
import TurnCard from "./components/TurnCard"
import { usePlanner } from "./usePlanner"
import "./App.css"

export default function App() {
  const { connected, turns, sendQuery, respondElicit, busy } = usePlanner()
  const [input, setInput] = useState("")
  const scrollRef = useRef(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" })
  }, [turns])

  const submit = (e) => {
    e.preventDefault()
    if (busy) return
    sendQuery(input)
    setInput("")
  }

  return (
    <div className="app">
      <Sidebar />

      <main className="main">
        <header className="topbar">
          <h1>LLM Planner</h1>
          <span className={`conn-badge ${connected ? "conn-up" : "conn-down"}`}>
            <span className="conn-dot" />
            {connected ? "Connected" : "Disconnected"}
          </span>
        </header>

        <div className="turns" ref={scrollRef}>
          {turns.length === 0 && (
            <div className="empty-state">
              <p>Ask for something like:</p>
              <ul>
                <li>"create a vp for customer acme and then snapshot it"</li>
                <li>"list all my virtual platform templates"</li>
                <li>"find issues in the logs and generate a report"</li>
              </ul>
            </div>
          )}
          {turns.map((turn) => (
            <TurnCard key={turn.id} turn={turn} onRespondElicit={respondElicit} />
          ))}
        </div>

        <form className="composer" onSubmit={submit}>
          <input
            type="text"
            placeholder={connected ? "Type a request…" : "Connecting to backend…"}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={!connected || busy}
          />
          <button type="submit" disabled={!connected || busy || !input.trim()}>
            {busy ? "Running…" : "Send"}
          </button>
        </form>
      </main>
    </div>
  )
}
