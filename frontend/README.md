# Planner Frontend

A React + Vite chat UI for the [planner backend](../backend) — lets you type a
request, watch it get decomposed and matched against candidate skills, watch
each planned step execute, and answer any elicitation prompts the backend
raises for missing parameters, all live over a WebSocket.

## Running it

```bash
npm install
npm run dev      # http://localhost:5173
```

The backend must be running separately — see [../backend/README.md](../backend/README.md#web-frontend):

```bash
cd ../backend
uvicorn server:app --reload --port 8000
```

## Configuration

[.env](.env) points the app at the backend:

```
VITE_API_BASE=http://localhost:8000
VITE_WS_URL=ws://localhost:8000/ws/plan
```

Change these if the backend runs on a different host/port.

## How it works

`src/usePlanner.js` opens the WebSocket connection to `/ws/plan` and turns
each incoming event into UI state for the current turn:

| Event | UI effect |
|---|---|
| `subtasks` | Shows the decomposed sub-request chips. |
| `candidates` | Shows the retrieved candidate skills and their similarity scores. |
| `plan` | Records the ordered list of steps the LLM chose. |
| `step_start` / `step` | Renders each step with a running/ok/error status and its output. |
| `elicit` | Renders an inline prompt (`ElicitCard`) — text input, choice buttons, or a multi-field form, depending on `kind` — and blocks that step until you respond. |
| `done` / `error` | Finalizes the turn with the assistant's reply or an error message. |

Sending a query, or answering an elicitation prompt, sends a JSON message
back over the same socket (`{"type": "query", ...}` or
`{"type": "elicit_response", ...}`); the backend is the source of truth for
what's currently running, so the UI never predicts state ahead of the server.

## Project structure

```
src/
  api.js              REST helpers (fetchSkills, fetchHealth) + WS_URL
  usePlanner.js        WebSocket hook: connection state, turn history, sendQuery, respondElicit
  App.jsx              Layout: sidebar + chat panel + composer
  components/
    Sidebar.jsx        Searchable, categorized skill catalog (GET /api/skills)
    TurnCard.jsx        One request/response turn: subtasks, candidates, steps, reply
    StepRow.jsx          A single plan step's status/params/output
    ElicitCard.jsx       Inline prompt for a missing-parameter request
```

## Build

```bash
npm run build     # outputs to dist/
npm run preview   # serve the production build locally
```
