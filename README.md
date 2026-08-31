# Planner Prototype

A working prototype of the "planner layer" proposal: instead of hard-wiring a
system to always run one fixed pipeline, a user request is routed at runtime
to whichever skill(s) actually apply, in whatever order and combination the
request needs — with a chat UI on top to drive it interactively.

```
User query
    │
    ▼
1. Decompose the request into independent sub-requests (LLM)
    │
    ▼
2. Semantic retrieval — embed each sub-request, rank against the skill
   catalog, merge into one candidate shortlist
    │
    ▼
3. LLM planner — pick which candidate skill(s) to call, in what order,
   with what parameters, from the shortlist only
    │
    ▼
4. Execute each step through MCP — a tool can elicit a missing parameter
   from the user instead of failing outright
    │
    ▼
5. Results — every step reports its own ok/error status independently
```

See [backend/README.md](backend/README.md) for the full design writeup
(background, problem, architecture) and [backend/architecture.svg](backend/architecture.svg)
for the diagram.

## Project layout

| | |
|---|---|
| [backend/](backend/README.md) | Python: the planner engine (decompose → retrieve → plan → execute), the MCP tool server, the Gemini LLM client, and a FastAPI wrapper (`server.py`) exposing it over REST/WebSocket. |
| [frontend/](frontend/README.md) | React + Vite: a chat UI that talks to the backend over WebSocket — shows retrieval/plan/execution live and prompts for missing parameters inline. |

## Quick start

```bash
# 1. repo-root .env needs:
#    GOOGLE_API_KEY=...   (Gemini — see backend/llm/llm.py)
#    HF_TOKEN=...         (Hugging Face Inference API — used for skill-search embeddings)

# 2. backend
cd backend
pip install -r requirements.txt
python embed_skills.py build          # builds skills_index/ from skills_library.jsonl
uvicorn server:app --reload --port 8000

# 3. frontend (separate terminal)
cd frontend
npm install
npm run dev                            # http://localhost:5173
```

Or skip the frontend and drive the planner directly from the CLI:

```bash
cd backend
python planner.py "create a vp for customer acme and then snapshot it"
```

## Docs

- [backend/README.md](backend/README.md) — design writeup, architecture, CLI usage, API endpoint reference, repo layout.
- [frontend/README.md](frontend/README.md) — UI structure, WebSocket event/message reference, configuration.
