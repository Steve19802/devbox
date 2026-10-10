import asyncio
import contextlib
import json
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import uvicorn
from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager


# --- Settings (read once at startup from DEVBOX_MCP_* env vars) ---


def _warn(message: str) -> None:
    print(f"⚠️ --- {message} ---", flush=True)


def _clamp(value: int, lo: int, hi: int) -> int:
    return min(max(value, lo), hi)


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Parse an int env var; fall back to default when invalid, clamp to [lo, hi]."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        clamped = _clamp(default, lo, hi)
        if clamped != default:
            _warn(f"{name} default {default} out of range [{lo}, {hi}]; clamped to {clamped}")
        return clamped
    try:
        value = int(raw.strip())
    except ValueError:
        _warn(f"Invalid {name}={raw!r}; using default {default}")
        return _clamp(default, lo, hi)
    if value < lo or value > hi:
        clamped = _clamp(value, lo, hi)
        _warn(f"{name}={value} out of range [{lo}, {hi}]; clamped to {clamped}")
        return clamped
    return value


def _env_list(name: str) -> frozenset[str]:
    """Parse a comma-separated env var into a set, dropping empties."""
    raw = os.environ.get(name, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    protected_services: frozenset[str]
    default_timeout: int
    max_timeout: int
    max_output_kb: int
    max_procs: int
    max_log_mb: int

    @classmethod
    def from_env(cls) -> "Settings":
        max_timeout = _env_int("DEVBOX_MCP_MAX_TIMEOUT", 3600, 1, 3600)
        return cls(
            protected_services=_env_list("DEVBOX_MCP_PROTECTED_SERVICES"),
            # default_timeout is clamped against the (already capped) max_timeout
            default_timeout=_env_int("DEVBOX_MCP_DEFAULT_TIMEOUT", 180, 1, max_timeout),
            max_timeout=max_timeout,
            max_output_kb=_env_int("DEVBOX_MCP_MAX_OUTPUT_KB", 64, 1, 1024),
            max_procs=_env_int("DEVBOX_MCP_MAX_PROCS", 5, 1, 20),
            max_log_mb=_env_int("DEVBOX_MCP_MAX_LOG_MB", 10, 1, 100),
        )


SETTINGS = Settings.from_env()
print(
    "⚙️ --- DEVBOX MCP SETTINGS --- "
    f"default_timeout={SETTINGS.default_timeout} "
    f"max_timeout={SETTINGS.max_timeout} "
    f"max_output_kb={SETTINGS.max_output_kb} "
    f"max_procs={SETTINGS.max_procs} "
    f"max_log_mb={SETTINGS.max_log_mb} "
    f"protected_services={sorted(SETTINGS.protected_services)}",
    flush=True,
)

# Helper script streamed into target containers. Loaded once at startup.
PROC_SCRIPT_PATH = Path(__file__).with_name("devbox-proc.sh")
try:
    PROC_SCRIPT_BYTES: Optional[bytes] = PROC_SCRIPT_PATH.read_bytes()
except OSError as exc:
    PROC_SCRIPT_BYTES = None
    _warn(f"Could not read {PROC_SCRIPT_PATH}: {exc}; process tools will fail")


# --- Async docker CLI helper ---


class DockerError(RuntimeError):
    """Raised when a docker CLI invocation fails or times out."""


async def docker(
    *args: str,
    input: Optional[bytes] = None,
    timeout: float = 30,
    merge_stderr: bool = False,
) -> tuple[int, bytes, bytes]:
    """Run `docker <args...>` without a shell and return (rc, stdout, stderr)."""
    stdin = asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL
    stderr_target = (
        asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
    )
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_target,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input), timeout=timeout
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise DockerError(
            f"docker {' '.join(args)} timed out after {timeout:g}s"
        ) from None
    return process.returncode or 0, stdout, stderr or b""


def _require_ok(rc: int, stderr: bytes, what: str) -> None:
    if rc != 0:
        detail = stderr.decode(errors="replace").strip()
        raise DockerError(f"{what} failed (exit {rc}): {detail}")


# --- Scope policy ---


@dataclass(frozen=True)
class ContainerInfo:
    service: str
    id: str
    name: str
    status: str
    running: bool
    started_at: str
    health: Optional[str]
    restart_count: int


def _labels(obj: dict) -> dict:
    return (obj.get("Config") or {}).get("Labels") or {}


def _networks(obj: dict) -> list[str]:
    networks = ((obj.get("NetworkSettings") or {}).get("Networks")) or {}
    if networks:
        return list(networks.keys())
    mode = (obj.get("HostConfig") or {}).get("NetworkMode")
    return [mode] if mode else []


async def _inspect_one(identifier: str) -> dict:
    rc, out, err = await docker("inspect", identifier)
    _require_ok(rc, err, f"docker inspect {identifier}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DockerError(f"invalid docker inspect JSON for {identifier}: {exc}") from None
    if not data:
        raise DockerError(f"docker inspect {identifier} returned no data")
    return data[0]


def _preferred(candidate: ContainerInfo, current: ContainerInfo) -> bool:
    """Prefer a running replica, then the alphabetically first name."""
    return (not candidate.running, candidate.name) < (not current.running, current.name)


async def _discover_containers() -> tuple[dict[str, ContainerInfo], dict[str, str]]:
    """Resolve the current scope: return (in-scope containers, deny reasons)."""
    self_info = await _inspect_one(socket.gethostname())
    self_id = self_info["Id"]
    project = _labels(self_info).get("com.docker.compose.project", "")
    if not project:
        raise DockerError("MCP container has no com.docker.compose.project label")

    # Networks this MCP container is attached to that belong to the project.
    self_networks = _networks(self_info)
    project_networks: set[str] = set()
    if self_networks:
        rc, out, err = await docker("network", "inspect", *self_networks)
        _require_ok(rc, err, "docker network inspect")
        try:
            networks = json.loads(out)
        except json.JSONDecodeError as exc:
            raise DockerError(f"invalid docker network inspect JSON: {exc}") from None
        for network in networks:
            labels = network.get("Labels") or {}
            if labels.get("com.docker.compose.project") == project:
                project_networks.add(network.get("Name", ""))

    # Candidate containers in the same compose project (including stopped ones).
    rc, out, err = await docker(
        "ps",
        "-a",
        "-q",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={project}",
    )
    _require_ok(rc, err, "docker ps")
    ids = out.decode(errors="replace").split()
    if not ids:
        return {}, {}

    rc, out, err = await docker("inspect", *ids)
    _require_ok(rc, err, "docker inspect")
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DockerError(f"invalid docker inspect JSON: {exc}") from None

    scoped: dict[str, ContainerInfo] = {}
    reasons: dict[str, str] = {}
    for obj in raw:
        labels = _labels(obj)
        service = labels.get("com.docker.compose.service", "")
        if not service:
            continue

        state = obj.get("State") or {}
        container = ContainerInfo(
            service=service,
            id=obj.get("Id", ""),
            name=(obj.get("Name") or "").lstrip("/"),
            status=state.get("Status", ""),
            running=bool(state.get("Running", False)),
            started_at=state.get("StartedAt", ""),
            health=(state.get("Health") or {}).get("Status"),
            restart_count=obj.get("RestartCount", 0),
        )

        if labels.get("com.docker.compose.project", "") != project:
            reasons.setdefault(service, "out of scope")
        elif container.id == self_id:
            reasons.setdefault(service, "out of scope")
        elif service in SETTINGS.protected_services:
            reasons[service] = "protected"
        elif not (set(_networks(obj)) & project_networks):
            reasons.setdefault(service, "out of scope")
        else:
            current = scoped.get(service)
            if current is None or _preferred(container, current):
                scoped[service] = container

    for service in scoped:
        reasons.pop(service, None)
    return scoped, reasons


async def get_scoped_containers() -> dict[str, ContainerInfo]:
    """Return all in-scope containers keyed by compose service name (fresh, no caching)."""
    scoped, _ = await _discover_containers()
    return scoped


async def resolve_scoped_container(service: str) -> Optional[ContainerInfo]:
    """Resolve a service name to an in-scope container, auditing the decision."""
    scoped, reasons = await _discover_containers()
    if service in scoped:
        info = scoped[service]
        print(
            f"🔎 --- SCOPE ALLOW [{service}] -> {info.name} ({info.id[:12]}) ---",
            flush=True,
        )
        return info
    reason = reasons.get(service, "unknown")
    print(f"⛔ --- SCOPE DENY [{service}] reason={reason} ---", flush=True)
    return None


# --- Output / argument helpers ---


def _parse_timeout(value: object) -> float:
    if value is None:
        return float(SETTINGS.default_timeout)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a number of seconds")
    if value < 1 or value > SETTINGS.max_timeout:
        raise ValueError(
            f"timeout must be between 1 and {SETTINGS.max_timeout} seconds"
        )
    return float(value)


def _truncate_output(data: bytes, max_kb: int) -> str:
    """Decode bytes, keeping only the last max_kb KB with a truncation note."""
    limit = max_kb * 1024
    if len(data) <= limit:
        return data.decode(errors="replace")
    total_kb = -(-len(data) // 1024)
    note = f"[truncated: showing last {max_kb} KB of {total_kb} KB]"
    return f"{note}\n{data[-limit:].decode(errors='replace')}"


# --- devbox-proc.sh runner (foreground + streaming) ---


async def run_proc_script(
    container_id: str,
    subcmd: str,
    *args: str,
    env: Optional[dict[str, str]] = None,
    timeout: float = 30,
) -> tuple[int, str, str]:
    """Stream devbox-proc.sh into a container and run a subcommand."""
    if PROC_SCRIPT_BYTES is None:
        raise DockerError(f"{PROC_SCRIPT_PATH} is not available")
    argv: list[str] = ["exec", "-i"]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += [container_id, "sh", "-s", "--", subcmd, *args]
    rc, out, err = await docker(*argv, input=PROC_SCRIPT_BYTES, timeout=timeout)
    return rc, out.decode(errors="replace"), err.decode(errors="replace")


_TIMEOUT_MESSAGE = (
    "Execution Timed Out ({seconds}s limit). The process group was terminated "
    "inside the container. For dev servers or long-running tasks use "
    "start_process instead."
)


async def _fgkill(container_id: str, token: str) -> None:
    try:
        rc, _, err = await run_proc_script(
            container_id, "fgkill", token, "5", timeout=15
        )
        if rc not in (0, 3):
            _warn(f"fgkill {token} returned exit {rc}: {err.strip()}")
    except Exception as exc:
        _warn(f"fgkill {token} failed: {exc}")


# --- Tool handlers ---


async def handle_execute_container_command(arguments: dict) -> str:
    container = arguments.get("container")
    command = arguments.get("command")
    if not isinstance(container, str) or not container.strip():
        return "Execution Error: 'container' must be a non-empty string."
    if not isinstance(command, str) or not command.strip():
        return "Execution Error: 'command' must be a non-empty string."
    try:
        timeout = _parse_timeout(arguments.get("timeout"))
    except ValueError as exc:
        return f"Execution Error: {exc}"

    info = await resolve_scoped_container(container)
    if info is None:
        return f"Service '{container}' not found or not allowed."
    if not info.running:
        return (
            f"Service '{container}' is not running (status: "
            f"{info.status or 'unknown'}). Start it first."
        )
    if PROC_SCRIPT_BYTES is None:
        return (
            "Execution Error: devbox-proc.sh is not available to the MCP "
            "server; cannot execute commands."
        )

    token = "fg-" + secrets.token_hex(6)
    print(f"\n🤖 --- AI EXECUTING IN [{container}] ---", flush=True)
    print(f"Command: {command}", flush=True)

    # DEVBOX_CMD is passed as its own argv element => no shell interpolation.
    argv = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"DEVBOX_CMD={command}",
        info.id,
        "sh",
        "-s",
        "--",
        "fg",
        token,
    ]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(PROC_SCRIPT_BYTES), timeout=timeout
        )
    except asyncio.TimeoutError:
        await _fgkill(info.id, token)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        message = _TIMEOUT_MESSAGE.format(seconds=int(timeout))
        print(f"❌ {message}", flush=True)
        print("-------------------------------------------\n", flush=True)
        return message

    returncode = process.returncode or 0
    stdout = _truncate_output(stdout_bytes, SETTINGS.max_output_kb)
    stderr = _truncate_output(stderr_bytes, SETTINGS.max_output_kb)

    print(f"Exit Code: {returncode}", flush=True)
    if stdout:
        print(f"STDOUT:\n{stdout.strip()}", flush=True)
    if stderr:
        print(f"STDERR:\n{stderr.strip()}", flush=True)
    print("-------------------------------------------\n", flush=True)

    if returncode == 5 and "devbox-proc:" in stderr:
        return (
            "Execution Error: devbox-proc.sh could not run in the container "
            f"(exit 5): {stderr.strip()}"
        )

    return f"Exit Code: {returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"


# --- Process tools: ids, validation, error mapping ---


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SINCE_RE = re.compile(r"^[0-9][0-9A-Za-z:.+-]{0,39}$")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:31].strip("-")


def _new_process_id(name: Optional[str]) -> str:
    token = secrets.token_hex(4)
    slug = _slugify(name) if name else ""
    return f"{slug}-{token}" if slug else token


def _require_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{label}' must be a non-empty string.")
    return value


def _validate_int(value: object, default: int, lo: int, hi: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{label}' must be an integer")
    if value < lo or value > hi:
        raise ValueError(f"'{label}' must be between {lo} and {hi}")
    return value


def _validate_number(
    value: object, default: float, lo: float, hi: float, label: str
) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{label}' must be a number")
    if value < lo or value > hi:
        raise ValueError(f"'{label}' must be between {lo:g} and {hi:g}")
    return float(value)


async def _resolve_running(service: str) -> tuple[Optional[ContainerInfo], Optional[str]]:
    """Resolve a service that must be in scope and running."""
    info = await resolve_scoped_container(service)
    if info is None:
        return None, f"Service '{service}' not found or not allowed."
    if not info.running:
        return None, (
            f"Service '{service}' is not running (status: "
            f"{info.status or 'unknown'}). Start it first."
        )
    return info, None


def _proc_error(rc: int, stderr: str, container: str, pid: str) -> Optional[str]:
    """Map devbox-proc.sh exit codes to an agent-facing message (None on success)."""
    if rc == 0:
        return None
    detail = stderr.strip()
    if rc == 2:
        return f"Execution Error: {detail or 'invalid input'}"
    if rc == 3:
        return f"Process '{pid}' not found in '{container}'."
    if rc == 4:
        return f"Execution Error: process '{pid}' already exists in '{container}'."
    if rc == 5:
        return (
            f"Execution Error: 'setsid' is not available in '{container}'. "
            "Install util-linux (or busybox) in that image."
        )
    return f"Execution Error: {detail or f'devbox-proc.sh exited with {rc}'}"


def _parse_logs(out: str) -> tuple[dict, str]:
    """Split the `logs` output into (JSON header dict, raw content)."""
    header, _, content = out.partition("\n")
    try:
        meta = json.loads(header)
    except json.JSONDecodeError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), content


def _line_with(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end]


async def _ps_records(container_id: str) -> list[dict]:
    rc, out, err = await run_proc_script(container_id, "ps")
    if rc != 0:
        raise DockerError(err.strip() or f"devbox-proc.sh ps exited with {rc}")
    records: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


async def _find_record(container_id: str, pid: str) -> Optional[dict]:
    try:
        for rec in await _ps_records(container_id):
            if rec.get("id") == pid:
                return rec
    except DockerError:
        return None
    return None


async def _wait_for_ready(
    container_id: str, pid: str, pattern: "re.Pattern[str]", ready_timeout: float
) -> tuple[str, Optional[str], Optional[int]]:
    """Poll logs incrementally until the regex matches, the process ends, or timeout.

    Returns (outcome, matched_line, exit_code); outcome is one of
    "ready" | "exited" | "lost" | "timeout".
    """
    offset = 0
    buffer = b""
    deadline = asyncio.get_running_loop().time() + ready_timeout
    while True:
        rc, out, _ = await run_proc_script(container_id, "logs", pid, "0", str(offset))
        if rc == 0:
            meta, content = _parse_logs(out)
            try:
                offset = int(meta.get("next_offset", offset))
            except (TypeError, ValueError):
                pass
            status = str(meta.get("status", "running"))
            # Rolling 64 KB window so a match can span chunk boundaries.
            buffer = (buffer + content.encode("utf-8", errors="replace"))[-65536:]
            text = buffer.decode("utf-8", errors="replace")
            match = pattern.search(text)
            if match:
                return "ready", _line_with(text, match.start()), None
            if status in ("exited", "lost"):
                record = await _find_record(container_id, pid)
                exit_code = record.get("exit_code") if record else None
                return status, None, exit_code if isinstance(exit_code, int) else None
        if asyncio.get_running_loop().time() >= deadline:
            return "timeout", None, None
        await asyncio.sleep(1)


async def handle_start_process(arguments: dict) -> str:
    try:
        container = _require_str(arguments.get("container"), "container")
        command = _require_str(arguments.get("command"), "command")
        name = arguments.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("'name' must be a string")
        pattern = None
        raw_pattern = arguments.get("ready_pattern")
        if raw_pattern is not None:
            if not isinstance(raw_pattern, str):
                raise ValueError("'ready_pattern' must be a string")
            try:
                pattern = re.compile(raw_pattern)
            except re.error as exc:
                return f"Execution Error: invalid ready_pattern regex: {exc}"
        ready_timeout = _validate_number(
            arguments.get("ready_timeout"), 60.0, 1, 600, "ready_timeout"
        )
    except ValueError as exc:
        return f"Execution Error: {exc}"

    info, error = await _resolve_running(container)
    if error:
        return error

    ref = name.strip() if isinstance(name, str) and name.strip() else None
    pid = _new_process_id(ref)
    if not _ID_RE.match(pid):
        pid = secrets.token_hex(4)

    rc, out, err = await run_proc_script(info.id, "count")
    if rc != 0:
        return _proc_error(rc, err, container, pid) or "Execution Error: could not count processes."
    try:
        running_count = int(out.strip() or "0")
    except ValueError:
        running_count = 0
    if running_count >= SETTINGS.max_procs:
        return (
            f"Process limit reached ({running_count}/{SETTINGS.max_procs} running in "
            f"'{container}'). Use list_processes to inspect or stop_process to free a slot."
        )

    env = {
        "DEVBOX_CMD": command,
        "DEVBOX_PROC_NAME": ref or pid,
        "DEVBOX_MAX_LOG_BYTES": str(SETTINGS.max_log_mb * 1024 * 1024),
    }
    rc, out, err = await run_proc_script(info.id, "start", pid, env=env)
    mapped = _proc_error(rc, err, container, pid)
    if mapped:
        return mapped
    meta, _ = _parse_logs(out)

    print(f"\n🚀 --- START_PROCESS [{container}] id={pid} pid={meta.get('pid')} ---", flush=True)
    print(f"Command: {command}", flush=True)

    if pattern is None:
        outcome_line = "Started; running detached (no ready_pattern)."
    else:
        outcome, matched_line, exit_code = await _wait_for_ready(
            info.id, pid, pattern, ready_timeout
        )
        if outcome == "ready":
            outcome_line = (
                f"READY: matched /{pattern.pattern}/ on: {matched_line.strip()!r}"
            )
        elif outcome in ("exited", "lost"):
            suffix = f" (exit code {exit_code})" if exit_code is not None else ""
            outcome_line = f"{outcome.upper()} before the ready pattern matched{suffix}."
        else:
            outcome_line = (
                f"NOT READY after {int(ready_timeout)}s; the process is still running."
            )

    tail_meta, tail_content = _parse_logs(
        (await run_proc_script(info.id, "logs", pid, "20"))[1]
    )
    return (
        f"Started process '{pid}' in '{container}'.\n{outcome_line}\n\n"
        f"Recent log (last 20 lines):\n{tail_content.strip() or '(no output yet)'}\n\n"
        f"Read more with get_process_output(container='{container}', id='{pid}') "
        f"(next_offset={tail_meta.get('next_offset')}); stop it with "
        f"stop_process(container='{container}', id='{pid}')."
    )


async def handle_list_processes(arguments: dict) -> str:
    container = arguments.get("container")
    if container is not None and (not isinstance(container, str) or not container.strip()):
        return "Execution Error: 'container' must be a non-empty string."

    if container:
        info, error = await _resolve_running(container)
        if error:
            return error
        targets = [info]
    else:
        scoped = await get_scoped_containers()
        targets = sorted(
            (c for c in scoped.values() if c.running), key=lambda c: c.service
        )
        if not targets:
            return "[]"

    # One failing container must not hide the others: collect per-container results.
    results = await asyncio.gather(
        *(_ps_records(c.id) for c in targets), return_exceptions=True
    )
    records: list[dict] = []
    for info, result in zip(targets, results):
        if isinstance(result, BaseException):
            records.append({"container": info.service, "error": str(result)})
            continue
        for rec in result:
            records.append({**rec, "container": info.service})
    return json.dumps(records, indent=2)


async def handle_get_process_output(arguments: dict) -> str:
    container = arguments.get("container")
    pid = arguments.get("id")
    if not isinstance(container, str) or not container.strip():
        return "Execution Error: 'container' must be a non-empty string."
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        return f"Execution Error: invalid process id {pid!r}."
    try:
        tail = _validate_int(arguments.get("tail"), 200, 1, 2000, "tail")
        offset = arguments.get("offset")
        if offset is not None:
            offset = _validate_int(offset, 0, 0, 10**12, "offset")
    except ValueError as exc:
        return f"Execution Error: {exc}"

    info, error = await _resolve_running(container)
    if error:
        return error

    proc_args = ["logs", pid, str(tail)]
    if offset is not None:
        proc_args.append(str(offset))
    rc, out, err = await run_proc_script(info.id, *proc_args)
    mapped = _proc_error(rc, err, container, pid)
    if mapped:
        return mapped

    meta, content = _parse_logs(out)
    data = content.encode("utf-8", errors="replace")
    truncated = len(data) > SETTINGS.max_output_kb * 1024
    header = (
        f"Process '{pid}' in '{container}': status={meta.get('status')} "
        f"next_offset={meta.get('next_offset')} log_base={meta.get('log_base')}"
    )
    if truncated and offset is not None:
        header += (
            "\n[note: earlier bytes were skipped by truncation; next_offset is "
            "still a valid absolute offset to resume from]"
        )
    return f"{header}\n\n{_truncate_output(data, SETTINGS.max_output_kb)}"


async def handle_stop_process(arguments: dict) -> str:
    container = arguments.get("container")
    pid = arguments.get("id")
    if not isinstance(container, str) or not container.strip():
        return "Execution Error: 'container' must be a non-empty string."
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        return f"Execution Error: invalid process id {pid!r}."
    try:
        grace = _validate_int(arguments.get("grace"), 10, 0, 60, "grace")
    except ValueError as exc:
        return f"Execution Error: {exc}"

    info, error = await _resolve_running(container)
    if error:
        return error

    rc, out, err = await run_proc_script(
        info.id, "stop", pid, str(grace), timeout=grace + 20
    )
    mapped = _proc_error(rc, err, container, pid)
    if mapped:
        return mapped
    meta, _ = _parse_logs(out)
    print(f"\n🛑 --- STOP_PROCESS [{container}] id={pid} ---", flush=True)
    return (
        f"Stopped process '{pid}' in '{container}': "
        f"status={meta.get('status')}, exit_code={meta.get('exit_code')}."
    )


# --- Service tools ---


async def handle_list_services(arguments: dict) -> str:
    scoped = await get_scoped_containers()
    services = [
        {
            "service": c.service,
            "container": c.name,
            "status": c.status,
            "running": c.running,
            "health": c.health,
            "started_at": c.started_at,
            "restart_count": c.restart_count,
        }
        for c in sorted(scoped.values(), key=lambda c: c.service)
    ]
    return json.dumps(services, indent=2)


async def handle_service_logs(arguments: dict) -> str:
    service = arguments.get("service")
    if not isinstance(service, str) or not service.strip():
        return "Execution Error: 'service' must be a non-empty string."
    try:
        tail = _validate_int(arguments.get("tail"), 200, 1, 5000, "tail")
    except ValueError as exc:
        return f"Execution Error: {exc}"
    since = arguments.get("since")
    if since is not None and (not isinstance(since, str) or not _SINCE_RE.match(since)):
        return (
            "Execution Error: invalid 'since' (expected e.g. '10m' or "
            "'2026-10-10T10:00:00')."
        )

    info = await resolve_scoped_container(service)
    if info is None:
        return f"Service '{service}' not found or not allowed."

    log_args = ["logs", "--tail", str(tail)]
    if since:
        log_args += ["--since", since]
    log_args.append(info.id)
    rc, out, err = await docker(*log_args, merge_stderr=True, timeout=60)
    if rc != 0:
        detail = err.decode(errors="replace").strip() or out.decode(errors="replace").strip()
        return f"Execution Error: docker logs failed (exit {rc}): {detail}"
    print(
        f"\n📜 --- SERVICE_LOGS [{service}] --tail {tail}"
        f"{' --since ' + since if since else ''} ---",
        flush=True,
    )
    return (
        f"Container logs for '{service}' (stdout+stderr):\n\n"
        f"{_truncate_output(out, SETTINGS.max_output_kb)}"
    )


async def _service_action(service: object, action: str, past: str) -> str:
    if not isinstance(service, str) or not service.strip():
        return "Execution Error: 'service' must be a non-empty string."
    info = await resolve_scoped_container(service)
    if info is None:
        return f"Service '{service}' not found or not allowed."
    print(
        f"\n🔄 --- SERVICE {action.upper()} [{service}] -> {info.name} "
        f"({info.id[:12]}) ---",
        flush=True,
    )
    if action == "restart":
        argv = ["restart", "-t", "10", info.id]
    elif action == "stop":
        argv = ["stop", "-t", "10", info.id]
    else:
        argv = ["start", info.id]
    rc, _out, err = await docker(*argv, timeout=60)
    if rc != 0:
        return (
            f"Execution Error: docker {action} failed "
            f"(exit {rc}): {err.decode(errors='replace').strip()}"
        )
    new_info = await resolve_scoped_container(service)
    if new_info is None:
        return f"Service '{service}' not found or not allowed after {action}."
    return (
        f"{past} service '{service}': container={new_info.name}, "
        f"status={new_info.status}, running={new_info.running}."
    )


async def handle_restart_service(arguments: dict) -> str:
    return await _service_action(arguments.get("service"), "restart", "Restarted")


async def handle_stop_service(arguments: dict) -> str:
    return await _service_action(arguments.get("service"), "stop", "Stopped")


async def handle_start_service(arguments: dict) -> str:
    return await _service_action(arguments.get("service"), "start", "Started")


# --- Tool definitions + registry ---


def build_execute_container_command_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="execute_container_command",
        description=(
            "Execute a bash/shell command inside a specific Docker container."
            "CRITICAL PATH MAPPING: Commands run inside the target container's /app/ directory. "
            "Any files you create at /app/ will appear in your local workspace under src/<service_name>/. "
            "For dev servers, watchers or anything long-running use start_process instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "enum": service_names,
                    "description": "Target container name",
                },
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Maximum execution time in seconds "
                        f"(default: {SETTINGS.default_timeout}, "
                        f"max: {SETTINGS.max_timeout})"
                    ),
                },
            },
            "required": ["container", "command"],
        },
    )


def _enum_prop(service_names: list[str], description: str) -> dict:
    return {"type": "string", "enum": service_names, "description": description}


_KILL_WARNING = (
    "WARNING: stopping or restarting kills any processes started with start_process in "
    "that container (they will then show exited/lost). Rebuilds, re-creation and "
    "`docker compose down` are NOT available here; ask the user to run devbox-up or "
    "devbox-rebuild on the host."
)


def build_start_process_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="start_process",
        description=(
            "Start a long-running command (dev server, watcher, build) detached in a "
            "running container. Returns immediately with a generated process id; "
            "optionally waits for a ready_pattern in the output. Monitor with "
            "list_processes / get_process_output and stop with stop_process."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "container": _enum_prop(service_names, "Target service (container) name"),
                "command": {
                    "type": "string",
                    "description": "Shell command to run detached in the container",
                },
                "name": {
                    "type": "string",
                    "description": "Optional label; becomes part of the process id",
                },
                "ready_pattern": {
                    "type": "string",
                    "description": "Optional Python regex to wait for in the process output",
                },
                "ready_timeout": {
                    "type": "number",
                    "description": "Seconds to wait for ready_pattern (default: 60, max: 600)",
                },
            },
            "required": ["container", "command"],
        },
    )


def build_list_processes_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="list_processes",
        description=(
            "List supervised processes (JSON records with container, id, name, cmd, pid, "
            "status, exit_code, started_at, uptime). Omit container to list across all "
            "running services."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "container": _enum_prop(
                    service_names, "Optional service; omit for all running services"
                )
            },
        },
    )


def build_get_process_output_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="get_process_output",
        description=(
            "Read the log of a process started with start_process. Returns a header "
            "(status, next_offset) then content; pass offset=next_offset to fetch only "
            "new output since the last read."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "container": _enum_prop(service_names, "Service running the process"),
                "id": {
                    "type": "string",
                    "description": "Process id returned by start_process",
                },
                "tail": {
                    "type": "integer",
                    "description": "Lines from the end (default: 200, max: 2000)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Absolute byte offset to read from; use next_offset to page forward",
                },
            },
            "required": ["container", "id"],
        },
    )


def build_stop_process_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="stop_process",
        description=(
            "Stop a process started with start_process (SIGTERM its process group, then "
            "SIGKILL after grace). Returns its final status and exit_code."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "container": _enum_prop(service_names, "Service running the process"),
                "id": {
                    "type": "string",
                    "description": "Process id returned by start_process",
                },
                "grace": {
                    "type": "integer",
                    "description": "Seconds to wait before SIGKILL (default: 10, max: 60)",
                },
            },
            "required": ["container", "id"],
        },
    )


def build_list_services_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="list_services",
        description=(
            "List in-scope compose services (JSON) including stopped containers: service, "
            "container name, status, running, health, started_at, restart_count."
        ),
        inputSchema={"type": "object", "properties": {}},
    )


def build_service_logs_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="service_logs",
        description=(
            "Show a container's stdout+stderr from the start of the container (docker "
            "logs), useful to inspect startup logs. Works on stopped containers too. "
            "Output is truncated to the last max_output_kb."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service": _enum_prop(service_names, "Service whose container logs to read"),
                "tail": {
                    "type": "integer",
                    "description": "Lines from the end (default: 200, max: 5000)",
                },
                "since": {
                    "type": "string",
                    "description": "Only logs since a duration or timestamp, e.g. '10m' or '2026-10-10T10:00:00'",
                },
            },
            "required": ["service"],
        },
    )


def build_restart_service_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="restart_service",
        description=f"Restart a service's container (docker restart -t 10). {_KILL_WARNING}",
        inputSchema={
            "type": "object",
            "properties": {"service": _enum_prop(service_names, "Service to restart")},
            "required": ["service"],
        },
    )


def build_stop_service_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="stop_service",
        description=f"Stop a service's container (docker stop -t 10). {_KILL_WARNING}",
        inputSchema={
            "type": "object",
            "properties": {"service": _enum_prop(service_names, "Service to stop")},
            "required": ["service"],
        },
    )


def build_start_service_tool(service_names: list[str]) -> Tool:
    return Tool(
        name="start_service",
        description=(
            "Start a stopped service's container (docker start). This does NOT rebuild or "
            "re-create it; for code/config changes ask the user to run devbox-up or "
            "devbox-rebuild on the host."
        ),
        inputSchema={
            "type": "object",
            "properties": {"service": _enum_prop(service_names, "Service to start")},
            "required": ["service"],
        },
    )


# Extend by appending a builder here and a handler to TOOLS below.
TOOL_BUILDERS: list[Callable[[list[str]], Tool]] = [
    build_execute_container_command_tool,
    build_start_process_tool,
    build_list_processes_tool,
    build_get_process_output_tool,
    build_stop_process_tool,
    build_list_services_tool,
    build_service_logs_tool,
    build_restart_service_tool,
    build_stop_service_tool,
    build_start_service_tool,
]

TOOLS: dict[str, Callable[[dict], Awaitable[str]]] = {
    "execute_container_command": handle_execute_container_command,
    "start_process": handle_start_process,
    "list_processes": handle_list_processes,
    "get_process_output": handle_get_process_output,
    "stop_process": handle_stop_process,
    "list_services": handle_list_services,
    "service_logs": handle_service_logs,
    "restart_service": handle_restart_service,
    "stop_service": handle_stop_service,
    "start_service": handle_start_service,
}


def build_tool_definitions(service_names: list[str]) -> list[Tool]:
    return [builder(service_names) for builder in TOOL_BUILDERS]


# Initialize the manual Server
mcp_server = Server("devbox-supervisor")


@mcp_server.list_tools()
async def handle_list_tools() -> list[Tool]:
    try:
        service_names = sorted(await get_scoped_containers())
    except Exception as exc:
        _warn(f"Service discovery failed: {exc}")
        service_names = []
    if not service_names:
        service_names = ["error_fetching_services"]
    return build_tool_definitions(service_names)


@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        handler = TOOLS.get(name)
        if handler is None:
            return [TextContent(type="text", text=f"Execution Error: Tool {name} not found")]
        text = await handler(arguments)
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return [TextContent(type="text", text=f"Execution Error: {str(e)}")]


# --- HTTP Transport Setup ---
# Legacy MCP HTTP+SSE transport (OpenCode v1): GET /sse opens the event stream,
# POST /messages carries JSON-RPC.
sse = SseServerTransport("/messages")

# MCP Streamable HTTP transport (OpenCode v2 `type: "remote"`): POST/GET/DELETE /mcp.
streamable = StreamableHTTPSessionManager(app=mcp_server)
_streamable_stack = contextlib.AsyncExitStack()


async def app(scope, receive, send):
    """
    Pure ASGI application.
    Bypasses all Starlette/FastMCP framework bugs by wiring Uvicorn directly to the MCP SDK.
    Exposes both the legacy SSE transport (/sse, /messages) and Streamable HTTP (/mcp).
    """
    if scope["type"] == "lifespan":
        # StreamableHTTPSessionManager needs its task group running for the app lifetime.
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await _streamable_stack.enter_async_context(streamable.run())
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
            elif message["type"] == "lifespan.shutdown":
                await _streamable_stack.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return
        return

    if scope["type"] != "http":
        return

    path = scope["path"]
    method = scope["method"]

    if path == "/sse" and method == "GET":
        # Let the MCP SDK handle the SSE stream completely natively
        async with sse.connect_sse(scope, receive, send) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )

    elif path == "/messages" and method == "POST":
        # Let the MCP SDK handle the POST message natively (prevents the Starlette NoneType crash)
        await sse.handle_post_message(scope, receive, send)

    elif path == "/mcp":
        # Streamable HTTP transport used by OpenCode v2 remote MCP servers
        await streamable.handle_request(scope, receive, send)

    else:
        # Fallback for invalid routes
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"Not Found"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000)
