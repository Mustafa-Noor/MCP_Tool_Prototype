"""
tools.py

Prototype tool implementations for the skill-driven planner architecture,
targeting a Theia-IDE-based embedded development environment backed by
virtual platforms (VPs) — i.e. simulated hardware targets you provision,
flash, debug, and run inside the IDE.

Each skill declared in `skills_library.jsonl` (the file you'll embed into
your vector store) maps to one entry in SKILL_REGISTRY here. The planner
picks a skill id (via semantic match + LLM selection), and the local MCP
environment looks it up in this registry and runs it.

This is a prototype: skills operate against simple in-memory state stores
(VP_STORE, WORKSPACE_STORE, FILE_STORE, SNAPSHOT_STORE) so that a plan of
several steps behaves consistently end to end (create a VP, flash it,
attach a debugger, build a workspace, commit it, ...) without needing a
real backend. State resets whenever the process restarts. Replace the
stores and step bodies with real integrations as you build this out.
"""

from __future__ import annotations

import hashlib
import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------

@dataclass
class Skill:
    id: str
    name: str
    steps: list[str]
    run: Callable[[dict[str, Any]], dict[str, Any]]


SKILL_REGISTRY: dict[str, Skill] = {}


def register_skill(skill: Skill) -> None:
    SKILL_REGISTRY[skill.id] = skill


def get_skill(skill_id: str) -> Skill:
    if skill_id not in SKILL_REGISTRY:
        raise KeyError(f"No skill registered for id '{skill_id}'")
    return SKILL_REGISTRY[skill_id]


def run_skill(skill_id: str, params: dict[str, Any]) -> dict[str, Any]:
    skill = get_skill(skill_id)
    print(f"[tools] running skill '{skill.id}' with steps {skill.steps}")
    return skill.run(params)


def run_plan(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Execute an ordered plan produced by the LLM planner.

    plan = [
        {"skill_id": "create_vp", "params": {...}},
        {"skill_id": "flash_firmware", "params": {...}},
        {"skill_id": "attach_debugger", "params": {...}},
    ]
    """
    results = []
    for step in plan:
        skill_id = step["skill_id"]
        params = step.get("params", {})
        try:
            output = run_skill(skill_id, params)
            results.append({"skill_id": skill_id, "status": "ok", "output": output})
        except Exception as exc:  # noqa: BLE001 - prototype-level catch-all
            results.append({"skill_id": skill_id, "status": "error", "error": str(exc)})
    return results


# ---------------------------------------------------------------------------
# Shared in-memory state (prototype-only; resets on process restart)
# ---------------------------------------------------------------------------

VP_TEMPLATES: list[dict[str, Any]] = [
    {"template_id": "cortex-m4-devkit", "name": "Cortex-M4 Dev Kit", "arch": "ARMv7E-M", "clock_mhz": 168, "default_ram_mb": 256},
    {"template_id": "risc-v-soc", "name": "RISC-V SoC", "arch": "RV64GC", "clock_mhz": 400, "default_ram_mb": 512},
    {"template_id": "automotive-ecu", "name": "Automotive ECU", "arch": "ARMv8-A", "clock_mhz": 800, "default_ram_mb": 1024},
    {"template_id": "generic-linux-vp", "name": "Generic Linux VP", "arch": "x86_64", "clock_mhz": 2000, "default_ram_mb": 2048},
]

KNOWN_EXTENSIONS: list[dict[str, str]] = [
    {"extension_id": "theia-cpp", "name": "C/C++ Tools"},
    {"extension_id": "theia-python", "name": "Python Tools"},
    {"extension_id": "theia-debug", "name": "Debug Adapter"},
    {"extension_id": "theia-git", "name": "Git Integration"},
    {"extension_id": "theia-vp-console", "name": "Virtual Platform Console"},
]

WORKSPACE_TEMPLATES: dict[str, dict[str, str]] = {
    "blank": {},
    "cpp-embedded": {
        "README.md": "# Embedded Project\n",
        "src/main.c": "int main(void) {\n    return 0;\n}\n",
    },
    "python": {
        "README.md": "# Python Project\n",
        "main.py": "def main():\n    pass\n\n\nif __name__ == '__main__':\n    main()\n",
    },
}

VP_STORE: dict[str, dict[str, Any]] = {}
SNAPSHOT_STORE: dict[str, dict[str, Any]] = {}
WORKSPACE_STORE: dict[str, dict[str, Any]] = {}
FILE_STORE: dict[str, dict[str, str]] = {}

_DEBUG_PORT_COUNTER = itertools.count(3000)
_SOURCE_EXTENSIONS = (".c", ".cpp", ".cc", ".py", ".rs")

# Additional stores mirroring the real Innexis ANA REST API surface
# (jobs, vSoC definitions, installed extensions, license servers, env vars).
JOB_STORE: dict[str, dict[str, Any]] = {}
VSOC_DEFINITIONS: dict[str, dict[str, Any]] = {}
INSTALLED_EXTENSIONS: dict[str, dict[str, Any]] = {
    "theia-git": {"extension_id": "theia-git", "name": "Git Integration", "version": "1.4.0"},
}
LICENSE_STORE: dict[str, dict[str, Any]] = {}
ENV_VAR_STORE: dict[str, str] = {}
PRODUCT_VERSION = "ANA 2026.1.0-prototype"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _random_mac() -> str:
    suffix = uuid.uuid4().hex[:6]
    return "52:54:00:" + ":".join(suffix[i:i + 2] for i in range(0, 6, 2))


def _record_job(operation: str, target_id: str | None = None) -> str:
    job_id = _new_id("job")
    JOB_STORE[job_id] = {
        "job_id": job_id,
        "operation": operation,
        "target_id": target_id,
        "status": "completed",
        "created_at": _now(),
    }
    return job_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _get_template(template_id: str) -> dict[str, Any]:
    for template in VP_TEMPLATES:
        if template["template_id"] == template_id:
            return template
    raise KeyError(f"Unknown VP template '{template_id}'")


def _get_vp(vp_id: str) -> dict[str, Any]:
    _require(bool(vp_id) and vp_id in VP_STORE, f"No virtual platform found with id '{vp_id}'")
    return VP_STORE[vp_id]


def _get_workspace(workspace_id: str) -> dict[str, Any]:
    _require(bool(workspace_id) and workspace_id in WORKSPACE_STORE, f"No workspace found with id '{workspace_id}'")
    return WORKSPACE_STORE[workspace_id]


# ---------------------------------------------------------------------------
# create_vp
# ---------------------------------------------------------------------------

def _validate_config(params: dict[str, Any]) -> dict[str, Any]:
    _require(bool(params.get("customer_id")), "create_vp requires 'customer_id'")
    _require(bool(params.get("name")), "create_vp requires 'name'")
    template_id = params.get("template_id", "cortex-m4-devkit")
    template = _get_template(template_id)
    print(f"  -> validate_config: template='{template_id}' ok")
    return {"template": template}


def _provision_resources(params: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    vp_id = _new_id("vp")
    cores = int(params.get("cores", 2))
    memory_mb = int(params.get("memory_mb", template["default_ram_mb"]))
    print(f"  -> provision_resources: allocating {cores} core(s) / {memory_mb}MB for {vp_id}")
    return {"platform_id": vp_id, "cores": cores, "memory_mb": memory_mb}


def _register_platform(params: dict[str, Any], provisioned: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    vp_id = provisioned["platform_id"]
    VP_STORE[vp_id] = {
        "vp_id": vp_id,
        "customer_id": params["customer_id"],
        "name": params["name"],
        "description": params.get("description", ""),
        "platform_type": params.get("platform_type", "standard"),
        "template_id": template["template_id"],
        "arch": template["arch"],
        "clock_mhz": template["clock_mhz"],
        "cores": provisioned["cores"],
        "memory_mb": provisioned["memory_mb"],
        "status": "running",
        "firmware": None,
        "network": {"mode": "nat", "mac_address": _random_mac()},
        "created_at": _now(),
        "started_at": _now(),
    }
    job_id = _record_job("create_vp", vp_id)
    print(f"  -> register_platform: {vp_id} registered and running (job {job_id})")
    return {"status": "registered"}


def create_vp(params: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_config(params)
    provisioned = _provision_resources(params, validated["template"])
    _register_platform(params, provisioned, validated["template"])
    return {"platform_id": provisioned["platform_id"], "status": "created"}


register_skill(Skill(
    id="create_vp",
    name="Create VP",
    steps=["validate_config", "provision_resources", "register_platform"],
    run=create_vp,
))


# ---------------------------------------------------------------------------
# delete_vp
# ---------------------------------------------------------------------------

def delete_vp(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    vp_id = vp["vp_id"]
    print(f"  -> locate_vp: found {vp_id} (status={vp['status']})")
    print(f"  -> deprovision_resources: releasing {vp['cores']} core(s) / {vp['memory_mb']}MB")
    stale_snapshots = [sid for sid, snap in SNAPSHOT_STORE.items() if snap["vp_id"] == vp_id]
    for sid in stale_snapshots:
        del SNAPSHOT_STORE[sid]
    del VP_STORE[vp_id]
    VSOC_DEFINITIONS.pop(vp_id, None)
    _record_job("delete_vp", vp_id)
    print(f"  -> deregister_platform: removed {vp_id} and {len(stale_snapshots)} snapshot(s)")
    return {"platform_id": vp_id, "status": "deleted"}


register_skill(Skill(
    id="delete_vp",
    name="Delete VP",
    steps=["locate_vp", "deprovision_resources", "deregister_platform"],
    run=delete_vp,
))


# ---------------------------------------------------------------------------
# start_vp / stop_vp
# ---------------------------------------------------------------------------

def start_vp(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", vp["vp_id"])
    boot_time_ms = max(50, 400 - vp["cores"] * 20)
    print(f"  -> boot_platform: {vp['vp_id']} booting (~{boot_time_ms}ms)")
    vp["status"] = "running"
    vp["started_at"] = _now()
    print("  -> health_check: ok")
    return {"platform_id": vp["vp_id"], "status": "running", "boot_time_ms": boot_time_ms}


register_skill(Skill(
    id="start_vp",
    name="Start VP",
    steps=["locate_vp", "boot_platform", "health_check"],
    run=start_vp,
))


def stop_vp(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", vp["vp_id"])
    print("  -> graceful_shutdown")
    vp["status"] = "stopped"
    vp["stopped_at"] = _now()
    print("  -> deallocate_compute")
    return {"platform_id": vp["vp_id"], "status": "stopped"}


register_skill(Skill(
    id="stop_vp",
    name="Stop VP",
    steps=["locate_vp", "graceful_shutdown", "deallocate_compute"],
    run=stop_vp,
))


# ---------------------------------------------------------------------------
# snapshot_vp / restore_vp_snapshot
# ---------------------------------------------------------------------------

def snapshot_vp(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", vp["vp_id"])
    print("  -> freeze_state")
    snapshot_id = _new_id("snap")
    size_mb = round(vp["memory_mb"] * 0.35, 1)
    SNAPSHOT_STORE[snapshot_id] = {
        "snapshot_id": snapshot_id,
        "vp_id": vp["vp_id"],
        "state": dict(vp),
        "size_mb": size_mb,
        "created_at": _now(),
    }
    print(f"  -> write_snapshot: {snapshot_id} ({size_mb}MB)")
    return {"snapshot_id": snapshot_id, "vp_id": vp["vp_id"], "size_mb": size_mb}


register_skill(Skill(
    id="snapshot_vp",
    name="Snapshot VP",
    steps=["locate_vp", "freeze_state", "write_snapshot"],
    run=snapshot_vp,
))


def restore_vp_snapshot(params: dict[str, Any]) -> dict[str, Any]:
    snapshot_id = params.get("snapshot_id")
    _require(bool(snapshot_id) and snapshot_id in SNAPSHOT_STORE, f"No snapshot found with id '{snapshot_id}'")
    snapshot = SNAPSHOT_STORE[snapshot_id]
    print("  -> locate_snapshot", snapshot_id)
    print("  -> validate_snapshot: ok")
    vp_id = snapshot["vp_id"]
    restored_state = dict(snapshot["state"])
    restored_state["status"] = "running"
    restored_state["started_at"] = _now()
    VP_STORE[vp_id] = restored_state
    print(f"  -> apply_snapshot: state applied to {vp_id}")
    print("  -> resume_platform")
    return {"platform_id": vp_id, "status": "running", "restored_from": snapshot_id}


register_skill(Skill(
    id="restore_vp_snapshot",
    name="Restore VP Snapshot",
    steps=["locate_snapshot", "validate_snapshot", "apply_snapshot", "resume_platform"],
    run=restore_vp_snapshot,
))


# ---------------------------------------------------------------------------
# list_vp_templates
# ---------------------------------------------------------------------------

def list_vp_templates(params: dict[str, Any]) -> dict[str, Any]:
    arch_filter = params.get("arch")
    print("  -> query_template_catalog", {"arch": arch_filter} if arch_filter else {})
    templates = [t for t in VP_TEMPLATES if not arch_filter or t["arch"] == arch_filter]
    return {"templates": templates, "count": len(templates)}


register_skill(Skill(
    id="list_vp_templates",
    name="List VP Templates",
    steps=["query_template_catalog"],
    run=list_vp_templates,
))


# ---------------------------------------------------------------------------
# attach_debugger
# ---------------------------------------------------------------------------

def attach_debugger(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    _require(vp["status"] == "running", f"VP '{vp['vp_id']}' must be running to attach a debugger")
    print("  -> locate_vp", vp["vp_id"])
    port = next(_DEBUG_PORT_COUNTER)
    print(f"  -> open_debug_port: {port}")
    session_id = _new_id("dbg")
    print(f"  -> establish_session: {session_id} (gdb-remote)")
    return {"session_id": session_id, "vp_id": vp["vp_id"], "port": port, "protocol": "gdb-remote"}


register_skill(Skill(
    id="attach_debugger",
    name="Attach Debugger",
    steps=["locate_vp", "open_debug_port", "establish_session"],
    run=attach_debugger,
))


# ---------------------------------------------------------------------------
# flash_firmware
# ---------------------------------------------------------------------------

def flash_firmware(params: dict[str, Any]) -> dict[str, Any]:
    image_path = params.get("image_path")
    _require(bool(image_path), "flash_firmware requires 'image_path'")
    vp = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", vp["vp_id"])
    valid_ext = (".bin", ".elf", ".hex")
    _require(image_path.lower().endswith(valid_ext), f"Unsupported firmware image type for '{image_path}' (expected {valid_ext})")
    print("  -> validate_image: ok")
    checksum = hashlib.sha1(image_path.encode("utf-8")).hexdigest()[:12]
    vp["firmware"] = {"image_path": image_path, "checksum": checksum, "flashed_at": _now()}
    print(f"  -> load_image: {image_path}")
    print(f"  -> verify_checksum: {checksum}")
    _record_job("flash_firmware", vp["vp_id"])
    return {"vp_id": vp["vp_id"], "checksum": checksum, "status": "flashed"}


register_skill(Skill(
    id="flash_firmware",
    name="Flash Firmware",
    steps=["locate_vp", "validate_image", "load_image", "verify_checksum"],
    run=flash_firmware,
))


# ---------------------------------------------------------------------------
# run_simulation
# ---------------------------------------------------------------------------

def run_simulation(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    _require(vp["status"] == "running", f"VP '{vp['vp_id']}' must be running to simulate")
    duration_ms = int(params.get("duration_ms", 1000))
    print("  -> locate_vp", vp["vp_id"])
    print(f"  -> configure_run: duration={duration_ms}ms")
    cycles = duration_ms * vp["clock_mhz"] * 1000
    print(f"  -> execute_cycles: ~{cycles:,} cycles")
    trace = {"cycles": cycles, "duration_ms": duration_ms, "instructions_retired": int(cycles * 0.8)}
    print("  -> collect_trace")
    return {"vp_id": vp["vp_id"], "trace": trace}


register_skill(Skill(
    id="run_simulation",
    name="Run Simulation",
    steps=["locate_vp", "configure_run", "execute_cycles", "collect_trace"],
    run=run_simulation,
))


# ---------------------------------------------------------------------------
# clone_vp
# ---------------------------------------------------------------------------

def clone_vp(params: dict[str, Any]) -> dict[str, Any]:
    source = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", source["vp_id"])
    new_vp_id = _new_id("vp")
    clone = dict(source)
    clone.update({
        "vp_id": new_vp_id,
        "customer_id": params.get("customer_id", source["customer_id"]),
        "status": "running",
        "network": {"mode": "nat", "mac_address": _random_mac()},
        "created_at": _now(),
        "started_at": _now(),
    })
    VP_STORE[new_vp_id] = clone
    print(f"  -> duplicate_state: {source['vp_id']} -> {new_vp_id}")
    job_id = _record_job("clone_vp", new_vp_id)
    print(f"  -> track_job: {job_id}")
    return {"platform_id": new_vp_id, "cloned_from": source["vp_id"], "job_id": job_id, "status": "created"}


register_skill(Skill(
    id="clone_vp",
    name="Clone VP",
    steps=["locate_vp", "duplicate_state", "track_job"],
    run=clone_vp,
))


# ---------------------------------------------------------------------------
# vSoC definition: get / update / validate
# ---------------------------------------------------------------------------

def _default_vsoc_definition(vp: dict[str, Any]) -> dict[str, Any]:
    return {
        "vp_id": vp["vp_id"],
        "arch": vp["arch"],
        "cpus": [{"core": i, "type": vp["arch"]} for i in range(vp["cores"])],
        "memory_mb": vp["memory_mb"],
        "peripherals": [],
    }


def get_vsoc_definition(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", vp["vp_id"])
    definition = VSOC_DEFINITIONS.setdefault(vp["vp_id"], _default_vsoc_definition(vp))
    print("  -> read_vsoc_definition")
    return {"vp_id": vp["vp_id"], "vsoc_definition": definition}


register_skill(Skill(
    id="get_vsoc_definition",
    name="Get vSoC Definition",
    steps=["locate_vp", "read_vsoc_definition"],
    run=get_vsoc_definition,
))


def update_vsoc_definition(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    changes = params.get("changes", {})
    _require(bool(changes), "update_vsoc_definition requires 'changes'")
    print("  -> locate_vp", vp["vp_id"])
    definition = VSOC_DEFINITIONS.setdefault(vp["vp_id"], _default_vsoc_definition(vp))
    definition.update(changes)
    print(f"  -> apply_vsoc_changes: {list(changes)}")
    print("  -> save_vsoc_definition")
    return {"vp_id": vp["vp_id"], "vsoc_definition": definition, "status": "updated"}


register_skill(Skill(
    id="update_vsoc_definition",
    name="Update vSoC Definition",
    steps=["locate_vp", "apply_vsoc_changes", "save_vsoc_definition"],
    run=update_vsoc_definition,
))


def validate_vsoc_definition(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", vp["vp_id"])
    definition = VSOC_DEFINITIONS.setdefault(vp["vp_id"], _default_vsoc_definition(vp))
    print("  -> validate_schema")
    errors = []
    if not definition.get("cpus"):
        errors.append("at least one CPU is required")
    if not definition.get("memory_mb"):
        errors.append("memory_mb must be set")
    valid = not errors
    print(f"  -> report_result: {'valid' if valid else 'invalid'}")
    return {"vp_id": vp["vp_id"], "valid": valid, "errors": errors}


register_skill(Skill(
    id="validate_vsoc_definition",
    name="Validate vSoC Definition",
    steps=["locate_vp", "validate_schema", "report_result"],
    run=validate_vsoc_definition,
))


# ---------------------------------------------------------------------------
# get_vp_power / update_vp_network / download_console_logs
# ---------------------------------------------------------------------------

def get_vp_power(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", vp["vp_id"])
    power_state = "ON" if vp["status"] == "running" else "OFF"
    print(f"  -> read_power_state: {power_state}")
    return {"vp_id": vp["vp_id"], "power_state": power_state}


register_skill(Skill(
    id="get_vp_power",
    name="Get VP Power State",
    steps=["locate_vp", "read_power_state"],
    run=get_vp_power,
))


def update_vp_network(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    mode = params.get("mode", "nat")
    _require(mode in ("nat", "bridged", "host-only"), f"Unsupported network mode '{mode}'")
    print("  -> locate_vp", vp["vp_id"])
    network = vp.setdefault("network", {"mode": "nat", "mac_address": _random_mac()})
    network["mode"] = mode
    print(f"  -> apply_network_config: mode={mode}")
    print("  -> restart_network_adapter")
    return {"vp_id": vp["vp_id"], "network": network, "status": "updated"}


register_skill(Skill(
    id="update_vp_network",
    name="Update VP Network",
    steps=["locate_vp", "apply_network_config", "restart_network_adapter"],
    run=update_vp_network,
))


def download_console_logs(params: dict[str, Any]) -> dict[str, Any]:
    vp = _get_vp(params.get("vp_id"))
    print("  -> locate_vp", vp["vp_id"])
    print("  -> package_console_log")
    return {"vp_id": vp["vp_id"], "download_url": f"https://example.local/vps/{vp['vp_id']}/console.log"}


register_skill(Skill(
    id="download_console_logs",
    name="Download Console Logs",
    steps=["locate_vp", "package_console_log"],
    run=download_console_logs,
))


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------

def create_workspace(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    _require(bool(name), "create_workspace requires 'name'")
    template = params.get("template", "blank")
    _require(template in WORKSPACE_TEMPLATES, f"Unknown workspace template '{template}'")
    print("  -> allocate_workspace", {"name": name})
    workspace_id = _new_id("ws")
    WORKSPACE_STORE[workspace_id] = {
        "workspace_id": workspace_id,
        "name": name,
        "description": params.get("description", ""),
        "template": template,
        "extensions": [],
        "created_at": _now(),
        "last_opened_at": None,
    }
    FILE_STORE[workspace_id] = dict(WORKSPACE_TEMPLATES[template])
    print(f"  -> scaffold_project: {len(FILE_STORE[workspace_id])} starter file(s)")
    default_exts = ["theia-git"]
    if template == "cpp-embedded":
        default_exts.append("theia-cpp")
    elif template == "python":
        default_exts.append("theia-python")
    WORKSPACE_STORE[workspace_id]["extensions"] = default_exts
    print(f"  -> install_default_extensions: {default_exts}")
    return {"workspace_id": workspace_id, "status": "created", "files": list(FILE_STORE[workspace_id])}


register_skill(Skill(
    id="create_workspace",
    name="Create Workspace",
    steps=["allocate_workspace", "scaffold_project", "install_default_extensions"],
    run=create_workspace,
))


# ---------------------------------------------------------------------------
# open_workspace
# ---------------------------------------------------------------------------

def open_workspace(params: dict[str, Any]) -> dict[str, Any]:
    workspace = _get_workspace(params.get("workspace_id"))
    print("  -> locate_workspace", workspace["workspace_id"])
    print("  -> mount_filesystem")
    workspace["last_opened_at"] = _now()
    print(f"  -> activate_extensions: {workspace['extensions']}")
    return {
        "workspace_id": workspace["workspace_id"],
        "files": list(FILE_STORE.get(workspace["workspace_id"], {})),
        "extensions": workspace["extensions"],
    }


register_skill(Skill(
    id="open_workspace",
    name="Open Workspace",
    steps=["locate_workspace", "mount_filesystem", "activate_extensions"],
    run=open_workspace,
))


# ---------------------------------------------------------------------------
# install_extension
# ---------------------------------------------------------------------------

def install_extension(params: dict[str, Any]) -> dict[str, Any]:
    extension_id = params.get("extension_id")
    _require(bool(extension_id), "install_extension requires 'extension_id'")
    workspace = _get_workspace(params.get("workspace_id"))
    match = next((e for e in KNOWN_EXTENSIONS if e["extension_id"] == extension_id), None)
    _require(match is not None, f"Unknown extension '{extension_id}'")
    print("  -> resolve_extension", extension_id)
    INSTALLED_EXTENSIONS.setdefault(extension_id, {"extension_id": extension_id, "name": match["name"], "version": "1.0.0"})
    if extension_id in workspace["extensions"]:
        print("  -> download_extension: already installed, skipping")
    else:
        print("  -> download_extension: fetched")
        workspace["extensions"].append(extension_id)
    print("  -> activate_extension")
    return {"workspace_id": workspace["workspace_id"], "extension_id": extension_id, "status": "installed"}


register_skill(Skill(
    id="install_extension",
    name="Install Extension",
    steps=["resolve_extension", "download_extension", "activate_extension"],
    run=install_extension,
))


# ---------------------------------------------------------------------------
# list_extensions / uninstall_extension
# ---------------------------------------------------------------------------

def list_extensions(params: dict[str, Any]) -> dict[str, Any]:
    print("  -> query_installed_extensions")
    extensions = list(INSTALLED_EXTENSIONS.values())
    return {"extensions": extensions, "count": len(extensions)}


register_skill(Skill(
    id="list_extensions",
    name="List Extensions",
    steps=["query_installed_extensions"],
    run=list_extensions,
))


def uninstall_extension(params: dict[str, Any]) -> dict[str, Any]:
    extension_id = params.get("extension_id")
    _require(bool(extension_id) and extension_id in INSTALLED_EXTENSIONS, f"Extension '{extension_id}' is not installed")
    print("  -> locate_extension", extension_id)
    del INSTALLED_EXTENSIONS[extension_id]
    for workspace in WORKSPACE_STORE.values():
        if extension_id in workspace["extensions"]:
            workspace["extensions"].remove(extension_id)
    print("  -> remove_extension")
    return {"extension_id": extension_id, "status": "uninstalled"}


register_skill(Skill(
    id="uninstall_extension",
    name="Uninstall Extension",
    steps=["locate_extension", "remove_extension"],
    run=uninstall_extension,
))


# ---------------------------------------------------------------------------
# create_file
# ---------------------------------------------------------------------------

def create_file(params: dict[str, Any]) -> dict[str, Any]:
    file_path = params.get("file_path")
    _require(bool(file_path), "create_file requires 'file_path'")
    workspace = _get_workspace(params.get("workspace_id"))
    content = params.get("content", "")
    files = FILE_STORE.setdefault(workspace["workspace_id"], {})
    _require(file_path not in files, f"File '{file_path}' already exists in workspace '{workspace['workspace_id']}'")
    print("  -> allocate_file", file_path)
    files[file_path] = content
    print(f"  -> write_content: {len(content)} byte(s)")
    return {"workspace_id": workspace["workspace_id"], "file_path": file_path, "status": "created"}


register_skill(Skill(
    id="create_file",
    name="Create File",
    steps=["allocate_file", "write_content"],
    run=create_file,
))


# ---------------------------------------------------------------------------
# update_file
# ---------------------------------------------------------------------------

def _locate_file(params: dict[str, Any]) -> dict[str, Any]:
    workspace_id = params.get("workspace_id")
    file_path = params.get("file_path")
    _require(bool(workspace_id), "update_file requires 'workspace_id'")
    _require(bool(file_path), "update_file requires 'file_path'")
    files = FILE_STORE.get(workspace_id, {})
    _require(file_path in files, f"File '{file_path}' not found in workspace '{workspace_id}'")
    print("  -> locate_file", file_path)
    return {"workspace_id": workspace_id, "file_path": file_path, "current_content": files[file_path]}


def _apply_patch(located: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    workspace_id = located["workspace_id"]
    file_path = located["file_path"]
    if "content" in params:
        new_content = params["content"]
    else:
        changes = params.get("changes", {})
        new_content = located["current_content"] + "".join(f"\n{k}={v}" for k, v in changes.items())
    FILE_STORE[workspace_id][file_path] = new_content
    print(f"  -> apply_patch: {file_path} now {len(new_content)} byte(s)")
    return {"content": new_content}


def _validate_file(located: dict[str, Any]) -> dict[str, Any]:
    print("  -> validate_file: ok")
    return {"valid": True}


def _save_file(located: dict[str, Any]) -> dict[str, Any]:
    print(f"  -> save_file: {located['file_path']} saved")
    return {"saved": True}


def update_file(params: dict[str, Any]) -> dict[str, Any]:
    located = _locate_file(params)
    _apply_patch(located, params)
    _validate_file(located)
    _save_file(located)
    return {"workspace_id": located["workspace_id"], "file_path": located["file_path"], "status": "updated"}


register_skill(Skill(
    id="update_file",
    name="Update File",
    steps=["locate_file", "apply_patch", "validate_file", "save_file"],
    run=update_file,
))


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------

def delete_file(params: dict[str, Any]) -> dict[str, Any]:
    workspace_id = params.get("workspace_id")
    file_path = params.get("file_path")
    _require(bool(workspace_id), "delete_file requires 'workspace_id'")
    _require(bool(file_path), "delete_file requires 'file_path'")
    files = FILE_STORE.get(workspace_id, {})
    _require(file_path in files, f"File '{file_path}' not found in workspace '{workspace_id}'")
    print("  -> locate_file", file_path)
    del files[file_path]
    print("  -> remove_file")
    return {"workspace_id": workspace_id, "file_path": file_path, "status": "deleted"}


register_skill(Skill(
    id="delete_file",
    name="Delete File",
    steps=["locate_file", "remove_file"],
    run=delete_file,
))


# ---------------------------------------------------------------------------
# search_workspace
# ---------------------------------------------------------------------------

def search_workspace(params: dict[str, Any]) -> dict[str, Any]:
    workspace_id = params.get("workspace_id")
    query = params.get("query")
    _require(bool(workspace_id), "search_workspace requires 'workspace_id'")
    _require(bool(query), "search_workspace requires 'query'")
    files = FILE_STORE.get(workspace_id, {})
    print("  -> index_workspace", {"files": len(files)})
    needle = query.lower()
    matches = [
        {"file_path": file_path, "line": lineno, "text": line.strip()}
        for file_path, content in files.items()
        for lineno, line in enumerate(content.splitlines(), start=1)
        if needle in line.lower()
    ]
    print(f"  -> run_query: {len(matches)} match(es)")
    matches.sort(key=lambda m: m["file_path"])
    print("  -> rank_results")
    return {"query": query, "matches": matches, "count": len(matches)}


register_skill(Skill(
    id="search_workspace",
    name="Search Workspace",
    steps=["index_workspace", "run_query", "rank_results"],
    run=search_workspace,
))


# ---------------------------------------------------------------------------
# build_project
# ---------------------------------------------------------------------------

def build_project(params: dict[str, Any]) -> dict[str, Any]:
    workspace = _get_workspace(params.get("workspace_id"))
    workspace_id = workspace["workspace_id"]
    files = FILE_STORE.get(workspace_id, {})
    print("  -> resolve_toolchain", {"workspace_id": workspace_id})
    sources = [f for f in files if f.endswith(_SOURCE_EXTENSIONS)]
    _require(bool(sources), f"No source files found in workspace '{workspace_id}'")
    print(f"  -> compile_sources: {len(sources)} file(s)")
    artifact = f"{workspace_id}.out"
    size_kb = sum(len(files[f]) for f in sources) // 4 + 32
    print(f"  -> link_binary: {artifact} (~{size_kb}KB)")
    return {"workspace_id": workspace_id, "artifact": artifact, "size_kb": size_kb, "warnings": 0, "status": "built"}


register_skill(Skill(
    id="build_project",
    name="Build Project",
    steps=["resolve_toolchain", "compile_sources", "link_binary"],
    run=build_project,
))


# ---------------------------------------------------------------------------
# run_tests
# ---------------------------------------------------------------------------

def run_tests(params: dict[str, Any]) -> dict[str, Any]:
    workspace = _get_workspace(params.get("workspace_id"))
    workspace_id = workspace["workspace_id"]
    files = FILE_STORE.get(workspace_id, {})
    print("  -> discover_tests", {"workspace_id": workspace_id})
    test_files = [f for f in files if "test" in f.lower()]
    if not test_files:
        print("  -> execute_tests: none found")
        print("  -> collect_results")
        return {"workspace_id": workspace_id, "tests_found": 0, "passed": 0, "failed": 0}
    print(f"  -> execute_tests: {len(test_files)} file(s)")
    print("  -> collect_results")
    return {"workspace_id": workspace_id, "tests_found": len(test_files), "passed": len(test_files), "failed": 0}


register_skill(Skill(
    id="run_tests",
    name="Run Tests",
    steps=["discover_tests", "execute_tests", "collect_results"],
    run=run_tests,
))


# ---------------------------------------------------------------------------
# git_commit
# ---------------------------------------------------------------------------

def git_commit(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    _require(bool(message), "git_commit requires 'message'")
    workspace = _get_workspace(params.get("workspace_id"))
    workspace_id = workspace["workspace_id"]
    files = FILE_STORE.get(workspace_id, {})
    print("  -> stage_changes", {"files": len(files)})
    commit_hash = hashlib.sha1(f"{workspace_id}:{message}:{_now()}".encode("utf-8")).hexdigest()[:10]
    print(f"  -> create_commit: {commit_hash}")
    print("  -> update_history")
    return {"workspace_id": workspace_id, "commit_hash": commit_hash, "files_changed": len(files), "message": message}


register_skill(Skill(
    id="git_commit",
    name="Git Commit",
    steps=["stage_changes", "create_commit", "update_history"],
    run=git_commit,
))


# ---------------------------------------------------------------------------
# list_jobs / get_job
# ---------------------------------------------------------------------------

def list_jobs(params: dict[str, Any]) -> dict[str, Any]:
    status_filter = params.get("status")
    print("  -> query_jobs", {"status": status_filter} if status_filter else {})
    jobs = [j for j in JOB_STORE.values() if not status_filter or j["status"] == status_filter]
    return {"jobs": jobs, "count": len(jobs)}


register_skill(Skill(
    id="list_jobs",
    name="List Jobs",
    steps=["query_jobs"],
    run=list_jobs,
))


def get_job(params: dict[str, Any]) -> dict[str, Any]:
    job_id = params.get("job_id")
    _require(bool(job_id) and job_id in JOB_STORE, f"No job found with id '{job_id}'")
    print("  -> locate_job", job_id)
    return JOB_STORE[job_id]


register_skill(Skill(
    id="get_job",
    name="Get Job",
    steps=["locate_job"],
    run=get_job,
))


# ---------------------------------------------------------------------------
# download_bug_report
# ---------------------------------------------------------------------------

def download_bug_report(params: dict[str, Any]) -> dict[str, Any]:
    print("  -> collect_diagnostics")
    bundle = {"vp_count": len(VP_STORE), "workspace_count": len(WORKSPACE_STORE), "job_count": len(JOB_STORE)}
    print(f"  -> package_bundle: {bundle}")
    report_id = _new_id("bugreport")
    print(f"  -> write_archive: {report_id}.zip")
    return {"download_url": f"https://example.local/bug-reports/{report_id}.zip", "included": bundle}


register_skill(Skill(
    id="download_bug_report",
    name="Download Bug Report",
    steps=["collect_diagnostics", "package_bundle", "write_archive"],
    run=download_bug_report,
))


# ---------------------------------------------------------------------------
# add_license_server / list_licenses
# ---------------------------------------------------------------------------

def add_license_server(params: dict[str, Any]) -> dict[str, Any]:
    server = params.get("server")
    _require(bool(server), "add_license_server requires 'server'")
    _require(server not in LICENSE_STORE, f"License server '{server}' is already registered")
    print("  -> validate_server", server)
    port = int(params.get("port", 27000))
    LICENSE_STORE[server] = {"server": server, "port": port, "status": "connected", "added_at": _now()}
    print(f"  -> register_server: {server}:{port}")
    return {"server": server, "port": port, "status": "connected"}


register_skill(Skill(
    id="add_license_server",
    name="Add License Server",
    steps=["validate_server", "register_server"],
    run=add_license_server,
))


def list_licenses(params: dict[str, Any]) -> dict[str, Any]:
    print("  -> query_license_servers")
    servers = list(LICENSE_STORE.values())
    return {"servers": servers, "count": len(servers)}


register_skill(Skill(
    id="list_licenses",
    name="List Licenses",
    steps=["query_license_servers"],
    run=list_licenses,
))


# ---------------------------------------------------------------------------
# add_env_variable / list_env_variables
# ---------------------------------------------------------------------------

def add_env_variable(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    _require(bool(name), "add_env_variable requires 'name'")
    _require(name not in ENV_VAR_STORE, f"Environment variable '{name}' already exists")
    print("  -> validate_variable", name)
    ENV_VAR_STORE[name] = params.get("value", "")
    print(f"  -> store_variable: {name}")
    return {"name": name, "status": "added"}


register_skill(Skill(
    id="add_env_variable",
    name="Add Environment Variable",
    steps=["validate_variable", "store_variable"],
    run=add_env_variable,
))


def list_env_variables(params: dict[str, Any]) -> dict[str, Any]:
    print("  -> query_variables")
    return {"variables": dict(ENV_VAR_STORE), "count": len(ENV_VAR_STORE)}


register_skill(Skill(
    id="list_env_variables",
    name="List Environment Variables",
    steps=["query_variables"],
    run=list_env_variables,
))


# ---------------------------------------------------------------------------
# health_check_ping / get_version
# ---------------------------------------------------------------------------

def health_check_ping(params: dict[str, Any]) -> dict[str, Any]:
    print("  -> ping_server")
    return {"status": "ok", "timestamp": _now()}


register_skill(Skill(
    id="health_check_ping",
    name="Health Check Ping",
    steps=["ping_server"],
    run=health_check_ping,
))


def get_version(params: dict[str, Any]) -> dict[str, Any]:
    print("  -> read_version")
    return {"product": "Innexis ANA", "version": PRODUCT_VERSION}


register_skill(Skill(
    id="get_version",
    name="Get Version",
    steps=["read_version"],
    run=get_version,
))


# ---------------------------------------------------------------------------
# analyze_logs_pipeline
# ---------------------------------------------------------------------------

def _discover_logs(params: dict[str, Any]) -> dict[str, Any]:
    vp_id = params.get("vp_id")
    if vp_id:
        _get_vp(vp_id)
        log_files = [f"{vp_id}-console.log", f"{vp_id}-trace.log"]
    else:
        log_files = ["app.log", "error.log"]
    print("  -> discover_logs", {"log_files": log_files})
    return {"log_files": log_files}


def _extract_entries(params: dict[str, Any]) -> dict[str, Any]:
    log_files = params.get("log_files", [])
    print("  -> extract_entries", {"log_files": log_files})
    entries = [f"{lf}: no anomalies detected" for lf in log_files]
    return {"entries": entries}


def _llm_clean(params: dict[str, Any]) -> dict[str, Any]:
    print("  -> llm_clean", {"entry_count": len(params.get("entries", []))})
    return {"cleaned_entries": params.get("entries", [])}


def _final_check(params: dict[str, Any]) -> dict[str, Any]:
    cleaned = params.get("cleaned_entries", [])
    print("  -> final_check", {"entry_count": len(cleaned)})
    issues = [e for e in cleaned if "error" in e.lower() or "fail" in e.lower()]
    return {"issues": issues}


def _fuse_payload(params: dict[str, Any]) -> dict[str, Any]:
    issues = params.get("issues", [])
    summary = f"{len(issues)} issue(s) found." if issues else "No issues found."
    print("  -> fuse_payload", {"summary": summary})
    return {"summary": summary}


def analyze_logs_pipeline(params: dict[str, Any]) -> dict[str, Any]:
    discovered = _discover_logs(params)
    extracted = _extract_entries({**params, **discovered})
    cleaned = _llm_clean(extracted)
    checked = _final_check(cleaned)
    fused = _fuse_payload(checked)
    return {"issues": checked["issues"], "summary": fused["summary"]}


register_skill(Skill(
    id="analyze_logs_pipeline",
    name="Analyze Logs Pipeline",
    steps=["discover_logs", "extract_entries", "llm_clean", "final_check", "fuse_payload"],
    run=analyze_logs_pipeline,
))


# ---------------------------------------------------------------------------
# restart_service
# ---------------------------------------------------------------------------

def restart_service(params: dict[str, Any]) -> dict[str, Any]:
    service_name = params.get("service_name")
    _require(bool(service_name), "restart_service requires 'service_name'")
    vp_id = params.get("vp_id")
    if vp_id:
        _get_vp(vp_id)
    print("  -> identify_service", {"service_name": service_name, "vp_id": vp_id})
    print("  -> graceful_stop")
    print("  -> start_service")
    print("  -> health_check: ok")
    return {"service_name": service_name, "vp_id": vp_id, "status": "restarted"}


register_skill(Skill(
    id="restart_service",
    name="Restart Service",
    steps=["identify_service", "graceful_stop", "start_service", "health_check"],
    run=restart_service,
))


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------

def generate_report(params: dict[str, Any]) -> dict[str, Any]:
    report_type = params.get("report_type", "summary")
    print("  -> gather_data", {"report_type": report_type})
    if report_type == "fleet_status":
        data = {
            "vp_count": len(VP_STORE),
            "running": sum(1 for vp in VP_STORE.values() if vp["status"] == "running"),
            "workspace_count": len(WORKSPACE_STORE),
        }
    else:
        data = {"note": "no live data source configured for this report type"}
    print("  -> format_report", data)
    report_id = _new_id("report")
    print(f"  -> export_report: {report_id}")
    return {"report_url": f"https://example.local/reports/{report_id}.pdf", "report_type": report_type, "data": data}


register_skill(Skill(
    id="generate_report",
    name="Generate Report",
    steps=["gather_data", "format_report", "export_report"],
    run=generate_report,
))


# ---------------------------------------------------------------------------
# check_platform_status
# ---------------------------------------------------------------------------

def check_platform_status(params: dict[str, Any]) -> dict[str, Any]:
    vp_id = params.get("platform_id") or params.get("vp_id")
    vp = _get_vp(vp_id)
    print("  -> query_status", {"platform_id": vp_id})
    if vp["status"] == "running" and vp.get("started_at"):
        started = datetime.fromisoformat(vp["started_at"])
        uptime = datetime.now(timezone.utc) - started
        uptime_str = f"{uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m"
    else:
        uptime_str = "0h 0m"
    print("  -> aggregate_health")
    return {"platform_id": vp_id, "status": vp["status"], "uptime": uptime_str}


register_skill(Skill(
    id="check_platform_status",
    name="Check Platform Status",
    steps=["query_status", "aggregate_health"],
    run=check_platform_status,
))


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    vp_result = run_skill("create_vp", {
        "customer_id": "acme",
        "platform_type": "standard",
        "template_id": "cortex-m4-devkit",
    })
    print(vp_result)

    ws_result = run_skill("create_workspace", {"name": "acme-firmware", "template": "cpp-embedded"})
    print(ws_result)

    vp_id = vp_result["platform_id"]
    workspace_id = ws_result["workspace_id"]

    print(run_skill("flash_firmware", {"vp_id": vp_id, "image_path": "firmware.elf"}))
    print(run_skill("attach_debugger", {"vp_id": vp_id}))
    print(run_skill("build_project", {"workspace_id": workspace_id}))
    print(run_skill("git_commit", {"workspace_id": workspace_id, "message": "Initial scaffold"}))
    print(run_skill("check_platform_status", {"platform_id": vp_id}))
    print(run_skill("generate_report", {"report_type": "fleet_status"}))
