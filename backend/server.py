"""
server.py

FastAPI backend for the planner frontend. Wraps `planner.plan_and_run` (the
same decompose -> retrieve -> LLM-plan -> MCP-execute flow the CLI uses)
behind:

  - GET  /api/health   liveness check
  - GET  /api/skills   the full skill catalog, for a browsable sidebar
  - POST /api/plan     one-shot planning turn; any elicitation is auto-declined
                        (fine for skills that don't need interactive input)
  - WS   /ws/plan       interactive planning turn: streams progress events as
                        they happen and forwards each elicitation request to
                        the connected client, waiting for its answer before
                        the underlying tool call continues — this is what
                        lets the browser prompt the user the same way the
                        CLI's input() does.

Run (from this directory, so the relative skills_index/ path resolves):
    uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastmcp.client.elicitation import ElicitResult

import planner

SKILLS_FILE = Path(__file__).with_name("skills_library.jsonl")

app = FastAPI(title="LLM Planner API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _json_safe(obj: Any) -> Any:
    """Round-trip through json.dumps(default=str) so MCP result payloads
    (which may carry non-plain-JSON types) become directly WebSocket-sendable."""
    return json.loads(json.dumps(obj, default=str))


def _load_skill_catalog() -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    if SKILLS_FILE.exists():
        with SKILLS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    catalog.append(json.loads(line))
    return catalog


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/skills")
async def list_skills() -> dict[str, Any]:
    catalog = _load_skill_catalog()
    return {"skills": catalog, "count": len(catalog)}


async def _auto_decline_elicitation(message, response_type, params, context):
    """Elicitation handler for the one-shot REST endpoint: there's no one to
    ask, so any missing-parameter prompt is declined, same as blank CLI input."""
    return ElicitResult(action="decline")


@app.post("/api/plan")
async def run_plan(payload: dict[str, Any]) -> dict[str, Any]:
    query = (payload.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    try:
        outcome = await planner.plan_and_run(query, elicitation_handler=_auto_decline_elicitation)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as an error payload
        return {"error": str(exc)}
    return _json_safe(outcome)


class WebSocketElicitationHandler:
    """Forwards each MCP elicitation request to the connected browser and
    waits for its answer, mirroring planner._cli_elicitation_handler's
    scalar / pick-list / multi-field-form cases but over JSON messages
    instead of input()."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._counter = 0
        self._pending: dict[str, asyncio.Future] = {}

    def resolve(self, request_id: str, message: dict[str, Any]) -> None:
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(message)

    def cancel_all(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def __call__(self, message, response_type, params, context):
        schema = getattr(params, "requestedSchema", None) or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()

        self._counter += 1
        request_id = str(self._counter)
        choices: list[str] | None = None

        # A scalar/pick-list request is wrapped by fastmcp as a single "value" field.
        if list(properties.keys()) == ["value"]:
            value_schema = properties["value"]
            choices = value_schema.get("enum") or ([value_schema["const"]] if "const" in value_schema else None)
            payload = {
                "type": "elicit",
                "id": request_id,
                "kind": "choice" if choices else "text",
                "message": message,
            }
            if choices:
                payload["choices"] = choices
        elif properties:
            payload = {
                "type": "elicit",
                "id": request_id,
                "kind": "form",
                "message": message,
                "fields": [
                    {"name": name, "label": field_schema.get("title", name), "required": name in required}
                    for name, field_schema in properties.items()
                ],
            }
        else:
            payload = {"type": "elicit", "id": request_id, "kind": "text", "message": message}

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._ws.send_json(payload)
        try:
            response = await future
        except asyncio.CancelledError:
            return ElicitResult(action="decline")

        if response.get("action") == "decline":
            return ElicitResult(action="decline")

        if payload["kind"] == "choice":
            value = response.get("value")
            if value not in (choices or []):
                return ElicitResult(action="decline")
            return value
        if payload["kind"] == "form":
            return response.get("value") or {}
        return response.get("value")


@app.websocket("/ws/plan")
async def ws_plan(websocket: WebSocket) -> None:
    await websocket.accept()
    handler = WebSocketElicitationHandler(websocket)
    running = False

    async def run_query(text: str) -> None:
        nonlocal running
        running = True
        try:
            async def on_event(event: dict[str, Any]) -> None:
                await websocket.send_json(_json_safe(event))

            await planner.plan_and_run(text, elicitation_handler=handler, on_event=on_event)
        except Exception as exc:  # noqa: BLE001 - reported to the client, not raised
            await websocket.send_json({"type": "error", "message": str(exc)})
        finally:
            running = False

    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            if msg_type == "query":
                if running:
                    await websocket.send_json({"type": "error", "message": "A query is already running."})
                    continue
                asyncio.create_task(run_query((message.get("text") or "").strip()))
            elif msg_type == "elicit_response":
                handler.resolve(message.get("id"), message)
    except WebSocketDisconnect:
        handler.cancel_all()
