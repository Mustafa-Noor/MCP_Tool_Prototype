"""
mcp_server.py

FastMCP server that exposes every skill registered in `tools.py` (and
documented in `skills_library.jsonl`) as an individual MCP tool, so any
MCP-aware client — Claude Desktop, an MCP-enabled IDE, a Theia MCP panel,
or the semantic planner itself — can drive this Theia/virtual-platform
prototype directly.

Each wrapper below has a real, typed signature (not a generic params blob)
so the tool shows up in a client with proper argument names and types.
Wrappers just assemble a params dict and delegate to `tools.run_skill`,
which runs the same in-memory-state-backed logic used by the CLI planner.

Two meta tools are also exposed:
  - list_skills: returns the full skill catalog (id/name/category/description)
    for discovery, mirroring skills_library.jsonl.
  - run_plan: executes an ordered multi-step plan in one call, matching
    tools.run_plan's semantics (each step's success/failure is reported
    independently instead of aborting the whole plan).

Run:
    python mcp_server.py            # stdio transport (default, for MCP clients)
    python mcp_server.py --http     # streamable-http transport on :8000
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from pydantic import Field, create_model

import tools

SKILLS_FILE = Path(__file__).with_name("skills_library.jsonl")


def _load_skill_catalog() -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    if SKILLS_FILE.exists():
        with SKILLS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    catalog.append(json.loads(line))
    return catalog


_SKILL_CATALOG = _load_skill_catalog()
_DESCRIPTIONS = {entry["id"]: entry["description"] for entry in _SKILL_CATALOG}


def _desc(skill_id: str, fallback: str) -> str:
    return _DESCRIPTIONS.get(skill_id, fallback)


async def _require(
    ctx: Context,
    value: Any,
    field: str,
    message: str,
    response_type: type = str,
) -> Any:
    """Return `value` if already provided, otherwise elicit it from the connected client."""
    if value is not None:
        return value
    result = await ctx.elicit(message, response_type)
    if isinstance(result, AcceptedElicitation):
        return result.data
    if isinstance(result, DeclinedElicitation):
        raise ValueError(f"'{field}' is required but the user declined to provide it")
    raise ValueError(f"'{field}' is required but the request was cancelled")


async def _require_choice(
    ctx: Context,
    value: Any,
    field: str,
    message: str,
    choices: list[str],
    empty_message: str,
) -> Any:
    """Like `_require`, but offers the client a pick-list of known-valid ids instead
    of open-ended free text, so a made-up id can't reach the tool and fail deep
    inside it. Fails fast with `empty_message` if there's nothing to pick from."""
    if value is not None:
        return value
    if not choices:
        raise ValueError(empty_message)
    result = await ctx.elicit(message, choices)
    if isinstance(result, AcceptedElicitation):
        chosen = result.data
        if chosen not in choices:
            raise ValueError(f"'{chosen}' is not a valid {field}. Valid options: {', '.join(choices)}")
        return chosen
    if isinstance(result, DeclinedElicitation):
        raise ValueError(f"'{field}' is required but the user declined to provide it")
    raise ValueError(f"'{field}' is required but the request was cancelled")


async def _require_fields(
    ctx: Context,
    provided: dict[str, Any],
    fields: dict[str, tuple[type, str, Any]],
    message: str,
) -> dict[str, Any]:
    """Elicit several related flat fields together in a single form, instead of
    one round trip per field, whenever more than one of them is missing. Fields
    are given as name -> (type, description, default); pass default=... for a
    field that's actually required. Already-provided values are left untouched
    and excluded from the form.

    MCP elicitation schemas must stay flat (primitive fields, no nesting), which
    is exactly what a multi-field creation payload (name/description/etc.) is —
    unlike a free-form `changes`-style object, which can't be elicited this way.
    """
    missing = {name: spec for name, spec in fields.items() if provided.get(name) is None}
    if not missing:
        return provided
    model_fields = {
        name: (typ, Field(default=default, description=desc))
        for name, (typ, desc, default) in missing.items()
    }
    FieldsModel = create_model("ElicitedFields", **model_fields)
    result = await ctx.elicit(message, FieldsModel)
    if isinstance(result, AcceptedElicitation):
        return {**provided, **result.data.model_dump()}
    if isinstance(result, DeclinedElicitation):
        raise ValueError(f"Required field(s) ({', '.join(missing)}) declined by the user")
    raise ValueError(f"Required field(s) ({', '.join(missing)}) — the request was cancelled")


_NO_VPS_MESSAGE = "No virtual platforms exist yet. Create one first with create_vp."
_NO_WORKSPACES_MESSAGE = "No workspaces exist yet. Create one first with create_workspace."


mcp = FastMCP(
    name="ana-virtual-platform-planner",
    instructions=(
        "Prototype MCP server for a Theia-IDE embedded development environment. "
        "Exposes virtual platform (VP) lifecycle management, Theia workspace and "
        "file operations, build/debug, source control, diagnostics, licensing, "
        "and settings tools backed by an in-memory state store (tools.py). "
        "Call list_skills to browse the catalog, or run_plan to execute several "
        "steps in one call."
    ),
)


# ---------------------------------------------------------------------------
# Meta tools
# ---------------------------------------------------------------------------

@mcp.tool(description="Lists the full skill catalog (id, name, category, description, tags) available on this server.")
def list_skills() -> dict[str, Any]:
    return {"skills": _SKILL_CATALOG, "count": len(_SKILL_CATALOG)}


@mcp.tool(description="Executes an ordered multi-step plan in one call, e.g. [{'skill_id': 'create_vp', 'params': {...}}, ...]. Each step reports its own ok/error status instead of aborting the whole plan.")
def run_plan(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return tools.run_plan(plan)


# ---------------------------------------------------------------------------
# VP lifecycle
# ---------------------------------------------------------------------------

@mcp.tool(description=_desc("create_vp", "Creates a new virtual platform."))
async def create_vp(
    ctx: Context,
    customer_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    platform_type: str = "standard",
    template_id: str = "cortex-m4-devkit",
    cores: int = 2,
    memory_mb: int | None = None,
) -> dict[str, Any]:
    filled = await _require_fields(
        ctx,
        {"customer_id": customer_id, "name": name, "description": description},
        {
            "customer_id": (str, "Which customer is this VP for?", ...),
            "name": (str, "A short display name for this VP", ...),
            "description": (str, "Optional description of this VP", ""),
        },
        "A few details are needed to create this VP:",
    )
    params: dict[str, Any] = {
        "customer_id": filled["customer_id"],
        "name": filled["name"],
        "description": filled.get("description", ""),
        "platform_type": platform_type,
        "template_id": template_id,
        "cores": cores,
    }
    if memory_mb is not None:
        params["memory_mb"] = memory_mb
    return tools.run_skill("create_vp", params)


@mcp.tool(description=_desc("delete_vp", "Deletes an existing virtual platform."))
async def delete_vp(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP should be deleted?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("delete_vp", {"vp_id": vp_id})


@mcp.tool(description=_desc("start_vp", "Boots a stopped virtual platform."))
async def start_vp(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP should be started?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("start_vp", {"vp_id": vp_id})


@mcp.tool(description=_desc("stop_vp", "Gracefully shuts down a running virtual platform."))
async def stop_vp(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP should be stopped?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("stop_vp", {"vp_id": vp_id})


@mcp.tool(description=_desc("snapshot_vp", "Captures a point-in-time snapshot of a virtual platform."))
async def snapshot_vp(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP should be snapshotted?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("snapshot_vp", {"vp_id": vp_id})


@mcp.tool(description=_desc("restore_vp_snapshot", "Restores a virtual platform to a previously captured snapshot."))
async def restore_vp_snapshot(ctx: Context, snapshot_id: str | None = None) -> dict[str, Any]:
    snapshot_id = await _require_choice(
        ctx, snapshot_id, "snapshot_id", "Which snapshot should be restored?",
        list(tools.SNAPSHOT_STORE.keys()), "No VP snapshots exist yet. Create one first with snapshot_vp.",
    )
    return tools.run_skill("restore_vp_snapshot", {"snapshot_id": snapshot_id})


@mcp.tool(description=_desc("list_vp_templates", "Lists available virtual platform templates, optionally filtered by architecture."))
def list_vp_templates(arch: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if arch is not None:
        params["arch"] = arch
    return tools.run_skill("list_vp_templates", params)


@mcp.tool(description=_desc("attach_debugger", "Attaches a GDB-remote debug session to a running virtual platform."))
async def attach_debugger(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP should the debugger attach to?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("attach_debugger", {"vp_id": vp_id})


@mcp.tool(description=_desc("flash_firmware", "Flashes a firmware image (.bin, .elf, or .hex) onto a virtual platform."))
async def flash_firmware(ctx: Context, vp_id: str | None = None, image_path: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP should the firmware be flashed to?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    image_path = await _require(ctx, image_path, "image_path", "Path to the firmware image (.bin/.elf/.hex)?")
    return tools.run_skill("flash_firmware", {"vp_id": vp_id, "image_path": image_path})


@mcp.tool(description=_desc("run_simulation", "Runs a virtual platform for a given duration and collects an execution trace."))
async def run_simulation(ctx: Context, vp_id: str | None = None, duration_ms: int = 1000) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP should be simulated?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("run_simulation", {"vp_id": vp_id, "duration_ms": duration_ms})


@mcp.tool(description=_desc("clone_vp", "Creates a duplicate of an existing virtual platform."))
async def clone_vp(ctx: Context, vp_id: str | None = None, customer_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP should be cloned?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    params: dict[str, Any] = {"vp_id": vp_id}
    if customer_id is not None:
        params["customer_id"] = customer_id
    return tools.run_skill("clone_vp", params)


@mcp.tool(description=_desc("get_vsoc_definition", "Retrieves the vSoC hardware definition of a virtual platform."))
async def get_vsoc_definition(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP's vSoC definition do you want?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("get_vsoc_definition", {"vp_id": vp_id})


@mcp.tool(description=_desc("update_vsoc_definition", "Applies changes to the vSoC hardware definition of a virtual platform."))
async def update_vsoc_definition(ctx: Context, vp_id: str | None = None, changes: dict[str, Any] | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP's vSoC definition should be updated?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    if not changes:
        # `changes` is a free-form object, and MCP elicitation schemas only support
        # flat primitive fields, so an arbitrary dict can't be elicited. Fall back to
        # eliciting the one flat field that's actually meaningful to change here.
        memory_mb = await _require(ctx, None, "memory_mb", "How much memory (MB) should this VP have?", int)
        changes = {"memory_mb": memory_mb}
    return tools.run_skill("update_vsoc_definition", {"vp_id": vp_id, "changes": changes})


@mcp.tool(description=_desc("validate_vsoc_definition", "Validates the vSoC hardware definition of a virtual platform."))
async def validate_vsoc_definition(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP's vSoC definition should be validated?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("validate_vsoc_definition", {"vp_id": vp_id})


@mcp.tool(description=_desc("get_vp_power", "Reads the current power state (ON/OFF) of a virtual platform."))
async def get_vp_power(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP's power state do you want?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("get_vp_power", {"vp_id": vp_id})


@mcp.tool(description=_desc("update_vp_network", "Updates the network mode (nat, bridged, or host-only) of a virtual platform."))
async def update_vp_network(ctx: Context, vp_id: str | None = None, mode: str = "nat") -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP's network should be updated?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("update_vp_network", {"vp_id": vp_id, "mode": mode})


@mcp.tool(description=_desc("download_console_logs", "Packages and returns a download link for a virtual platform's console log."))
async def download_console_logs(ctx: Context, vp_id: str | None = None) -> dict[str, Any]:
    vp_id = await _require_choice(
        ctx, vp_id, "vp_id", "Which VP's console logs do you want?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("download_console_logs", {"vp_id": vp_id})


# ---------------------------------------------------------------------------
# Theia workspace
# ---------------------------------------------------------------------------

@mcp.tool(description=_desc("create_workspace", "Creates a new Theia IDE workspace scaffolded from a project template."))
async def create_workspace(
    ctx: Context, name: str | None = None, description: str | None = None, template: str = "blank"
) -> dict[str, Any]:
    filled = await _require_fields(
        ctx,
        {"name": name, "description": description},
        {
            "name": (str, "What should the new workspace be named?", ...),
            "description": (str, "Optional description of this workspace", ""),
        },
        "A few details are needed to create this workspace:",
    )
    return tools.run_skill(
        "create_workspace",
        {"name": filled["name"], "description": filled.get("description", ""), "template": template},
    )


@mcp.tool(description=_desc("open_workspace", "Opens an existing Theia IDE workspace and activates its extensions."))
async def open_workspace(ctx: Context, workspace_id: str | None = None) -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace should be opened?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    return tools.run_skill("open_workspace", {"workspace_id": workspace_id})


@mcp.tool(description=_desc("install_extension", "Installs and activates a Theia IDE extension into a workspace."))
async def install_extension(ctx: Context, workspace_id: str | None = None, extension_id: str | None = None) -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace should the extension be installed into?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    extension_id = await _require_choice(
        ctx, extension_id, "extension_id", "Which extension should be installed?",
        [e["extension_id"] for e in tools.KNOWN_EXTENSIONS], "No extensions are available to install.",
    )
    return tools.run_skill("install_extension", {"workspace_id": workspace_id, "extension_id": extension_id})


@mcp.tool(description=_desc("list_extensions", "Lists all Theia IDE extensions currently installed."))
def list_extensions() -> dict[str, Any]:
    return tools.run_skill("list_extensions", {})


@mcp.tool(description=_desc("uninstall_extension", "Uninstalls a Theia IDE extension by its id."))
async def uninstall_extension(ctx: Context, extension_id: str | None = None) -> dict[str, Any]:
    extension_id = await _require_choice(
        ctx, extension_id, "extension_id", "Which extension should be uninstalled?",
        list(tools.INSTALLED_EXTENSIONS.keys()), "No extensions are currently installed.",
    )
    return tools.run_skill("uninstall_extension", {"extension_id": extension_id})


@mcp.tool(description=_desc("create_file", "Creates a new file with optional initial content inside a Theia workspace."))
async def create_file(ctx: Context, workspace_id: str | None = None, file_path: str | None = None, content: str = "") -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace should the file be created in?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    file_path = await _require(ctx, file_path, "file_path", "What should the new file's path be?")
    return tools.run_skill("create_file", {"workspace_id": workspace_id, "file_path": file_path, "content": content})


@mcp.tool(description=_desc("update_file", "Updates or patches an existing file's content within a Theia workspace."))
async def update_file(
    ctx: Context,
    workspace_id: str | None = None,
    file_path: str | None = None,
    content: str | None = None,
    changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace is the file in?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    file_path = await _require_choice(
        ctx, file_path, "file_path", "Which file should be updated?",
        list(tools.FILE_STORE.get(workspace_id, {}).keys()), f"Workspace '{workspace_id}' has no files yet.",
    )
    params: dict[str, Any] = {"workspace_id": workspace_id, "file_path": file_path}
    if content is not None:
        params["content"] = content
    elif changes is not None:
        params["changes"] = changes
    return tools.run_skill("update_file", params)


@mcp.tool(description=_desc("delete_file", "Deletes an existing file from a Theia workspace."))
async def delete_file(ctx: Context, workspace_id: str | None = None, file_path: str | None = None) -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace is the file in?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    file_path = await _require_choice(
        ctx, file_path, "file_path", "Which file should be deleted?",
        list(tools.FILE_STORE.get(workspace_id, {}).keys()), f"Workspace '{workspace_id}' has no files yet.",
    )
    return tools.run_skill("delete_file", {"workspace_id": workspace_id, "file_path": file_path})


@mcp.tool(description=_desc("search_workspace", "Searches all files in a Theia workspace for a text query."))
async def search_workspace(ctx: Context, workspace_id: str | None = None, query: str | None = None) -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace should be searched?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    query = await _require(ctx, query, "query", "What should be searched for?")
    return tools.run_skill("search_workspace", {"workspace_id": workspace_id, "query": query})


# ---------------------------------------------------------------------------
# Build / debug / source control
# ---------------------------------------------------------------------------

@mcp.tool(description=_desc("build_project", "Compiles and links a Theia workspace's source files into a binary artifact."))
async def build_project(ctx: Context, workspace_id: str | None = None) -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace should be built?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    return tools.run_skill("build_project", {"workspace_id": workspace_id})


@mcp.tool(description=_desc("run_tests", "Discovers and executes test files within a Theia workspace."))
async def run_tests(ctx: Context, workspace_id: str | None = None) -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace's tests should be run?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    return tools.run_skill("run_tests", {"workspace_id": workspace_id})


@mcp.tool(description=_desc("git_commit", "Stages all changed files in a Theia workspace and creates a git commit."))
async def git_commit(ctx: Context, workspace_id: str | None = None, message: str | None = None) -> dict[str, Any]:
    workspace_id = await _require_choice(
        ctx, workspace_id, "workspace_id", "Which workspace should be committed?",
        list(tools.WORKSPACE_STORE.keys()), _NO_WORKSPACES_MESSAGE,
    )
    message = await _require(ctx, message, "message", "What should the commit message be?")
    return tools.run_skill("git_commit", {"workspace_id": workspace_id, "message": message})


# ---------------------------------------------------------------------------
# Jobs / operations
# ---------------------------------------------------------------------------

@mcp.tool(description=_desc("list_jobs", "Lists background jobs tracked by the platform, optionally filtered by status."))
def list_jobs(status: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if status is not None:
        params["status"] = status
    return tools.run_skill("list_jobs", params)


@mcp.tool(description=_desc("get_job", "Retrieves the details and status of a single background job by its id."))
async def get_job(ctx: Context, job_id: str | None = None) -> dict[str, Any]:
    job_id = await _require_choice(
        ctx, job_id, "job_id", "Which job id do you want details for?",
        list(tools.JOB_STORE.keys()), "No background jobs have run yet.",
    )
    return tools.run_skill("get_job", {"job_id": job_id})


@mcp.tool(description=_desc("download_bug_report", "Collects platform diagnostics and packages them into a downloadable bug report archive."))
def download_bug_report() -> dict[str, Any]:
    return tools.run_skill("download_bug_report", {})


@mcp.tool(description=_desc("health_check_ping", "Pings the platform backend to confirm it is reachable and responsive."))
def health_check_ping() -> dict[str, Any]:
    return tools.run_skill("health_check_ping", {})


@mcp.tool(description=_desc("get_version", "Returns the product name and version of the running platform."))
def get_version() -> dict[str, Any]:
    return tools.run_skill("get_version", {})


# ---------------------------------------------------------------------------
# Licensing / settings
# ---------------------------------------------------------------------------

@mcp.tool(description=_desc("add_license_server", "Registers a new license server (host and port) with the platform."))
async def add_license_server(ctx: Context, server: str | None = None, port: int = 27000) -> dict[str, Any]:
    server = await _require(ctx, server, "server", "What is the license server's host?")
    return tools.run_skill("add_license_server", {"server": server, "port": port})


@mcp.tool(description=_desc("list_licenses", "Lists all currently registered license servers and their connection status."))
def list_licenses() -> dict[str, Any]:
    return tools.run_skill("list_licenses", {})


@mcp.tool(description=_desc("add_env_variable", "Adds a new environment variable to the platform's settings. Fails if it already exists."))
async def add_env_variable(ctx: Context, name: str | None = None, value: str = "") -> dict[str, Any]:
    name = await _require(ctx, name, "name", "What should the environment variable be named?")
    return tools.run_skill("add_env_variable", {"name": name, "value": value})


@mcp.tool(description=_desc("list_env_variables", "Lists all environment variables currently configured on the platform."))
def list_env_variables() -> dict[str, Any]:
    return tools.run_skill("list_env_variables", {})


# ---------------------------------------------------------------------------
# Diagnostics / reporting
# ---------------------------------------------------------------------------

@mcp.tool(description=_desc("analyze_logs_pipeline", "Discovers, extracts, cleans, and summarizes issues from system, application, or VP console logs."))
def analyze_logs_pipeline(
    vp_id: str | None = None,
    log_source: str | None = None,
    time_range: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if vp_id is not None:
        params["vp_id"] = vp_id
    if log_source is not None:
        params["log_source"] = log_source
    if time_range is not None:
        params["time_range"] = time_range
    return tools.run_skill("analyze_logs_pipeline", params)


@mcp.tool(description=_desc("restart_service", "Restarts a running service or application component, optionally scoped to a virtual platform."))
async def restart_service(ctx: Context, service_name: str | None = None, vp_id: str | None = None) -> dict[str, Any]:
    service_name = await _require(ctx, service_name, "service_name", "Which service should be restarted?")
    params: dict[str, Any] = {"service_name": service_name}
    if vp_id is not None:
        params["vp_id"] = vp_id
    return tools.run_skill("restart_service", params)


@mcp.tool(description=_desc("generate_report", "Generates a formatted summary report, including a fleet-wide status report across all VPs and workspaces."))
def generate_report(report_type: str = "summary", data_source: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"report_type": report_type}
    if data_source is not None:
        params["data_source"] = data_source
    return tools.run_skill("generate_report", params)


@mcp.tool(description=_desc("check_platform_status", "Checks the current health, uptime, and status of a virtual platform."))
async def check_platform_status(ctx: Context, platform_id: str | None = None) -> dict[str, Any]:
    platform_id = await _require_choice(
        ctx, platform_id, "platform_id", "Which VP's status do you want to check?",
        list(tools.VP_STORE.keys()), _NO_VPS_MESSAGE,
    )
    return tools.run_skill("check_platform_status", {"platform_id": platform_id})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.run(transport="http", host="127.0.0.1", port=8000)
    else:
        mcp.run()
