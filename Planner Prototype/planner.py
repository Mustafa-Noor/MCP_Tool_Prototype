"""
planner.py

End-to-end semantic planner:
  1. Take a user query.
  2. Retrieve the top-K closest skills from the vector store (embed_skills.search).
  3. Hand the query + candidate skills to the LLM (utility.llm) and let it decide
     which skill(s) to call, in what order, and with what parameters.
  4. Execute the resulting plan step by step through an in-process MCP client
     against mcp_server.py, resolving any references to an earlier step's
     output along the way. Going through MCP (rather than calling tools.py
     directly) is what lets a tool elicit a missing required parameter from
     this CLI instead of just failing.

Usage:
    python planner.py                              # interactive prompt loop
    python planner.py "flash my firmware and start the debugger"
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

import mcp_server
from embed_skills import search
from utility.llm import extract_json_object, get_default_llm

TOP_K = 5
MAX_SUBTASKS = 4

# Presentation pacing: brief, deliberate pauses so a live demo reads as a sequence
# of steps rather than a wall of text dumped all at once.
_STEP_DELAY = 0.35
_LINE_DELAY = 0.15
_RULE = "-" * 60


def _pause(seconds: float = _STEP_DELAY) -> None:
    time.sleep(seconds)

# A compound request's single embedding vector dilutes toward neither intent, so
# a skill relevant to only one part of the request (e.g. "create a vp" inside
# "find the logs and also create a vp") can rank far outside top_k for the
# sentence as a whole even though it would rank highly on its own. Asking the
# LLM to break the request into a short todo-list of standalone sub-requests
# first, then retrieving candidates for each one separately, recovers that
# recall without hand-rolled splitting rules.
DECOMPOSE_SYSTEM_PROMPT = """Break the user's request into a short todo-list of its \
distinct, independent sub-requests, so each item names one self-contained action \
or question, reworded to stand alone (drop filler like "can you" / "please").

Rules:
- If the request is already a single ask, return a list with just that one item.
- Don't invent sub-requests that aren't implied by the text.
- Respond with ONLY a JSON object, no prose, in this exact shape:
{"subtasks": ["...", "..."]}
"""

PLANNER_SYSTEM_PROMPT = """You are a tool-selection planner for a Theia-IDE virtual-platform \
assistant. You will be given a user request and a shortlist of candidate skills \
(id, description, and input_schema). Decide which of the candidate skills (zero, \
one, or several, in the order they should run) are needed to satisfy the request, \
and what parameters to pass to each.

Rules:
- Bias toward action. If a candidate skill plausibly satisfies part or all of the \
request, call it — do not stop and ask a clarifying question instead of calling a \
tool. Missing information is handled by omitting that parameter (see below), not \
by refusing to plan.
- When the request's verb names an action a candidate skill performs (create, \
start, stop, delete, flash, run, attach, clone, snapshot, restore, install, \
build, commit, ...), call that skill — even if it has missing parameters and \
even if a different, higher-scoring *read-only/discovery* candidate (list_*, \
get_*, search_*) covers the same topic without needing those parameters. Never \
substitute a read-only lookup for the action the user actually asked for; the \
missing parameters will be elicited from the user when the action skill runs.
- Only use skill ids from the candidate list. Never invent a skill id.
- Only include parameters you can confidently infer from the request; omit ones \
you can't — do not guess values, and never invent an id (e.g. a vp_id or \
workspace_id) that wasn't given to you. The tool call still runs with the \
parameters you do have; missing ones will either use a sensible default or \
surface as a normal tool error, which is fine.
- A request naming several distinct actions (e.g. "find the logs and create a \
VP") should produce one step per action, in order — don't drop one of them just \
because it's easier to ask about.
- If the request needs no tool call at all (e.g. it's a greeting, small talk, or \
a question none of the candidate skills answer even partially), return an empty \
"steps" list and put a normal, direct conversational reply in "reply".
- If steps is non-empty, always set "reply" to null. You are planning *before* \
execution, so you don't yet know which omitted parameters will be filled in \
interactively (elicited from the user) once a step actually runs, which default \
was used, or whether a step will error — anything you write now about missing \
or assumed parameters risks contradicting what actually happens. The executed \
results speak for themselves; don't narrate over them.
- If a step needs a value produced by an earlier step, use the placeholder \
"<from:STEP_INDEX.FIELD>" as the value, where STEP_INDEX is the 0-based index of \
the earlier step and FIELD is a field name taken from that earlier skill's \
output_schema — never guess a field name that isn't listed there.
- Respond with ONLY a JSON object, no prose, in this exact shape:
{"steps": [{"skill_id": "...", "params": {...}}, ...], "reply": "..." | null}
"""


def _format_candidates(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        lines.append(json.dumps({
            "id": c["id"],
            "name": c["name"],
            "description": c["description"],
            "input_schema": c.get("input_schema", {}),
            "output_schema": c.get("output_schema", {}),
        }))
    return "\n".join(lines)


def _decompose_query(query: str, llm) -> list[str]:
    try:
        raw = llm.complete(system=DECOMPOSE_SYSTEM_PROMPT, user=query, temperature=0.0)
        subtasks = extract_json_object(raw).get("subtasks", [])
    except Exception as exc:  # noqa: BLE001 - decomposition is a recall aid, not required
        print(f"  (decomposition failed, falling back to single query: {exc})")
        return [query]
    subtasks = [s.strip() for s in subtasks if isinstance(s, str) and s.strip()]
    return subtasks[:MAX_SUBTASKS] or [query]


def _merge_candidates(candidate_lists: list[list[dict]], limit: int) -> list[dict]:
    best: dict[str, dict] = {}
    for candidates in candidate_lists:
        for c in candidates:
            existing = best.get(c["id"])
            if existing is None or c["similarity"] > existing["similarity"]:
                best[c["id"]] = c
    ranked = sorted(best.values(), key=lambda c: -c["similarity"])
    return ranked[:limit]


def _retrieve_candidates(query: str, top_k: int, llm) -> list[dict]:
    subtasks = _decompose_query(query, llm)
    if len(subtasks) > 1:
        print(f"  (decomposed into {len(subtasks)} sub-task(s): {subtasks})")
    with ThreadPoolExecutor(max_workers=len(subtasks)) as pool:
        candidate_lists = list(pool.map(lambda s: search(s, top_k=top_k), subtasks))
    return _merge_candidates(candidate_lists, limit=top_k * len(candidate_lists))


async def _cli_elicitation_handler(message, response_type, params, context):
    schema = getattr(params, "requestedSchema", None) or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()

    # A scalar/pick-list request is wrapped by fastmcp as a single "value" field.
    if list(properties.keys()) == ["value"]:
        value_schema = properties["value"]
        # A single-option pick-list comes back as "const" rather than "enum" (Pydantic
        # collapses a one-value Literal), so both need to be treated as a choice list.
        choices = value_schema.get("enum") or ([value_schema["const"]] if "const" in value_schema else None)

        if choices:
            print(f"  ? {message}")
            for i, choice in enumerate(choices, 1):
                print(f"      {i}. {choice}")
            while True:
                raw = input(f"    > (1-{len(choices)}, or blank to cancel) ").strip()
                if not raw:
                    return ElicitResult(action="decline")
                if raw.isdigit() and 1 <= int(raw) <= len(choices):
                    return choices[int(raw) - 1]
                if raw in choices:
                    return raw
                print(f"    Please enter a number between 1 and {len(choices)}, or one of the listed values.")

        answer = input(f"  ? {message} ").strip()
        if not answer:
            return ElicitResult(action="decline")
        return answer

    # A multi-field form (e.g. name + description together): prompt for each field
    # in turn and return them as one dict, instead of one round trip per field.
    if properties:
        print(f"  ? {message}")
        values: dict[str, str] = {}
        for field_name, field_schema in properties.items():
            label = field_schema.get("title", field_name)
            is_required = field_name in required
            suffix = "" if is_required else " (optional)"
            raw = input(f"      {label}{suffix}: ").strip()
            if not raw:
                if is_required:
                    return ElicitResult(action="decline")
                continue
            values[field_name] = raw
        return values

    # Empty-schema (no fields at all) elicitation.
    answer = input(f"  ? {message} ").strip()
    if not answer:
        return ElicitResult(action="decline")
    return answer


def _error_text(result) -> str:
    texts = [getattr(block, "text", None) for block in result.content]
    texts = [t for t in texts if t]
    return "; ".join(texts) if texts else "tool call failed"


def _resolve_placeholders(params: dict, prior_results: list[dict]) -> dict:
    resolved = {}
    for key, value in params.items():
        if isinstance(value, str) and value.startswith("<from:") and value.endswith(">"):
            step_idx_str, _, field = value[len("<from:"):-1].partition(".")
            try:
                resolved[key] = prior_results[int(step_idx_str)]["output"][field]
                continue
            except (ValueError, IndexError, KeyError, TypeError):
                pass  # can't resolve it — fall through and keep the literal placeholder
        resolved[key] = value
    return resolved


async def plan_and_run(query: str, top_k: int = TOP_K) -> dict:
    llm = get_default_llm()
    if llm is None:
        raise RuntimeError("No LLM configured — set an EDAI/FUSE credential (see utility/llm.py).")

    print(f"\n{_RULE}")
    print(f"Candidate skills for: {query!r}")
    candidates = _retrieve_candidates(query, top_k, llm)
    _pause()
    for c in candidates:
        print(f"  {c['similarity']:.4f}  {c['id']}")
        _pause(_LINE_DELAY)

    user_prompt = f"User request: {query}\n\nCandidate skills:\n{_format_candidates(candidates)}"
    raw = llm.complete(system=PLANNER_SYSTEM_PROMPT, user=user_prompt, temperature=0.0)
    plan = extract_json_object(raw)
    steps = plan.get("steps", [])
    # Enforced regardless of what the LLM returns: a reply drafted before execution
    # can't know what elicitation, defaults, or errors will happen once steps actually
    # run, so it risks contradicting the real results (see PLANNER_SYSTEM_PROMPT).
    reply = plan.get("reply") if not steps else None

    valid_ids = {c["id"] for c in candidates}
    results: list[dict] = []
    if steps:
        print(f"\nPlan ({len(steps)} step(s)):")
        _pause()

    async with Client(mcp_server.mcp, elicitation_handler=_cli_elicitation_handler) as client:
        for i, step in enumerate(steps):
            skill_id = step.get("skill_id")
            if skill_id not in valid_ids:
                print(f"  [{i}] {skill_id} -> rejected (not one of the candidate skills)")
                results.append({"skill_id": skill_id, "status": "error", "error": "skill_id not in candidate list"})
                _pause()
                continue

            params = _resolve_placeholders(step.get("params", {}), results)
            print(f"  [{i}] {skill_id}({params})")
            _pause()
            result = await client.call_tool(skill_id, params, raise_on_error=False)
            if result.is_error:
                error = _error_text(result)
                print(f"      -> error: {error}")
                results.append({"skill_id": skill_id, "status": "error", "error": error})
            else:
                results.append({"skill_id": skill_id, "status": "ok", "output": result.data})
            _pause()

    return {"reply": reply, "results": results}


if __name__ == "__main__":
    def _show(outcome: dict) -> None:
        if outcome["reply"]:
            print(f"\n{_RULE}")
            print(f"Assistant: {outcome['reply']}")
            _pause()
        if outcome["results"]:
            print(f"\n{_RULE}")
            print("Results:")
            _pause()
            print(json.dumps(outcome["results"], indent=2, default=str))
        print(f"{_RULE}\n")

    if len(sys.argv) > 1:
        _show(asyncio.run(plan_and_run(" ".join(sys.argv[1:]))))
    else:
        print(f"{_RULE}\nEnter a request (blank line to quit):\n{_RULE}")
        while True:
            user_query = input("\n> ").strip()
            if not user_query:
                break
            _show(asyncio.run(plan_and_run(user_query)))
