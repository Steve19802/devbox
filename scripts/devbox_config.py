"""Shared DevBox project-configuration helpers.

This module is the single source of truth for the ``devbox.json`` v2 layout. It
is imported by ``scaffold.py``, ``adopt.py`` and ``manage_service.py`` (all of
which run either on the host or inside a ``python:3.11-slim`` container).

Only the standard library is used so the module can be copied/imported anywhere.
"""

import copy
import json
import os
import re
import tempfile

SCHEMA_VERSION = 2

DEFAULT_COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.dev.yml",
    "build/docker-compose.mcp.yml",
]

# Default MCP process/service-control configuration (mirrors the ``mcp`` section
# of the v2 schema). Deep-copied wherever it is embedded.
DEFAULT_MCP_CONFIG = {
    "protected_services": [],
    "exec": {"default_timeout": 180, "max_timeout": 3600, "max_output_kb": 64},
    "processes": {"max_per_container": 5, "max_log_mb": 10},
}

# Compose file names that count as "this folder already has a project".
COMPOSE_CANDIDATES = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
]

MD_BEGIN = "<!-- >>> devbox >>> -->"
MD_END = "<!-- <<< devbox <<< -->"
HASH_BEGIN = "# >>> devbox >>>"
HASH_END = "# <<< devbox <<<"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_KNOWN_SERVICE_KEYS = (
    "name",
    "source",
    "workdir",
    "context",
    "dockerfile",
    "target",
    "port",
    "runtime",
    "managed",
)


class ConfigError(Exception):
    """Raised when a devbox.json (or service/path) is malformed."""


# --------------------------------------------------------------------------- #
# Validation / normalization helpers
# --------------------------------------------------------------------------- #
def sanitize_service_name(raw: str) -> str:
    """Lowercase ``raw`` and turn invalid characters into ``-``.

    Raises :class:`ConfigError` if the result is not a valid compose service
    name (``^[a-z0-9][a-z0-9_-]*$``).
    """
    if not isinstance(raw, str):
        raise ConfigError(
            f"service name must be a string, got {type(raw).__name__}"
        )
    name = re.sub(r"[^a-z0-9_-]", "-", raw.strip().lower())
    if not _NAME_RE.match(name):
        raise ConfigError(
            f"invalid service name {raw!r} (expected {_NAME_RE.pattern})"
        )
    return name


def normalize_rel_path(path: str, project_root: str) -> str:
    """Return ``path`` as a posix path relative to ``project_root``.

    Accepts relative paths (``./apps/web/``) or absolute paths inside the root.
    ``.`` is returned for the root itself. Raises :class:`ConfigError` when the
    path escapes the project root (``..`` or an outside absolute path).
    """
    if not isinstance(path, str) or not path.strip():
        raise ConfigError(f"invalid path {path!r}")
    root = os.path.abspath(project_root)
    if os.path.isabs(path):
        absolute = os.path.abspath(path)
    else:
        absolute = os.path.abspath(os.path.join(root, path))
    rel = os.path.relpath(absolute, root)
    if rel == ".":
        return "."
    if rel == ".." or rel.startswith(".." + os.sep):
        raise ConfigError(f"path {path!r} escapes the project root")
    return rel.replace(os.sep, "/")


def _coerce_port(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"invalid port {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            raise ConfigError(f"invalid port {value!r}") from None
    raise ConfigError(f"invalid port {value!r}")


def _normalize_service_v2(svc: dict, mode: str = "scaffold") -> dict:
    if not isinstance(svc, dict):
        raise ConfigError("each service must be a JSON object")
    name = sanitize_service_name(svc.get("name"))
    source = svc.get("source")
    if source is not None and not isinstance(source, str):
        raise ConfigError(f"service {name!r}: 'source' must be a string or null")
    default_workdir = "/app" if source is not None else None
    default_dockerfile = "Dockerfile" if source is not None else None

    out = {
        "name": name,
        "source": source,
        "workdir": svc.get("workdir", default_workdir),
        "context": svc.get("context", source),
        "dockerfile": svc.get("dockerfile", default_dockerfile),
        "target": svc.get("target"),
        "port": _coerce_port(svc.get("port")),
        "runtime": svc.get("runtime"),
        # Adopt services are user-owned unless they explicitly say otherwise.
        "managed": bool(svc.get("managed", mode != "adopt")),
    }
    # Keep any per-service keys we do not know about (forward compatibility).
    for key, value in svc.items():
        if key not in _KNOWN_SERVICE_KEYS:
            out[key] = value
    return out


def _normalize_v1_scaffold_service(svc: dict) -> dict:
    if not isinstance(svc, dict):
        raise ConfigError("each service must be a JSON object")
    name = sanitize_service_name(svc.get("name"))
    return {
        "name": name,
        "source": f"src/{name}",
        "workdir": "/app",
        "context": f"src/{name}",
        "dockerfile": "Dockerfile",
        "target": "dev",
        "port": _coerce_port(svc.get("port")),
        "runtime": svc.get("runtime"),
        "managed": True,
    }


def _normalize_v1_adopt(raw: dict) -> dict:
    name = sanitize_service_name(raw.get("service"))
    context = raw.get("dockerfile_context") or "."
    stages = raw.get("stages")
    has_dev = isinstance(stages, dict) and "dev" in stages
    dockerfile = raw.get("dockerfile") or "Dockerfile"
    return {
        "name": name,
        # v1 adopt mounted the whole project at /app, so "." is accurate.
        "source": ".",
        "workdir": "/app",
        "context": context,
        "dockerfile": dockerfile,
        "target": "dev" if has_dev else None,
        "port": _coerce_port(raw.get("port")),
        "runtime": None,
        "managed": False,
    }


def make_service(
    name,
    source,
    workdir="/app",
    context=None,
    dockerfile="Dockerfile",
    target=None,
    port=None,
    runtime=None,
    managed=True,
) -> dict:
    """Build a v2 service dict, applying the documented defaults.

    ``context`` defaults to ``source``. When ``source`` is ``None`` (an
    image-only service) ``workdir``/``context``/``dockerfile`` are all ``None``.
    """
    if source is None:
        workdir = None
        context = None
        dockerfile = None
    elif context is None:
        context = source
    return {
        "name": name,
        "source": source,
        "workdir": workdir,
        "context": context,
        "dockerfile": dockerfile,
        "target": target,
        "port": port,
        "runtime": runtime,
        "managed": managed,
    }


def new_config(mode: str, services: list[dict], inject_ide: bool = False, extra=None) -> dict:
    """Build a fresh v2 config for ``services``.

    ``extra`` (optional) is merged over the generated defaults, which lets
    callers keep adopt-only keys such as ``preserve_env``.
    """
    if mode not in ("scaffold", "adopt"):
        raise ConfigError(f"unknown mode {mode!r} (expected 'scaffold' or 'adopt')")
    cfg = {
        "version": SCHEMA_VERSION,
        "mode": mode,
        "services": [dict(svc) for svc in services],
        "inject_ide": bool(inject_ide),
        "compose_files": list(DEFAULT_COMPOSE_FILES),
        "mcp": copy.deepcopy(DEFAULT_MCP_CONFIG),
    }
    if extra:
        cfg.update(extra)
    return normalize_config(cfg)


def normalize_config(raw: dict) -> dict:
    """Normalize any (v1 or v2) config into the v2 schema.

    Unknown top-level keys are preserved. v1 adopt/scaffold shapes are upgraded.
    Raises :class:`ConfigError` for structurally invalid input.
    """
    if not isinstance(raw, dict):
        raise ConfigError("devbox.json must be a JSON object")

    version = raw.get("version", 1)
    services = raw.get("services")

    if "service" in raw and not services:
        # v1 adopt shape: a single service described at the top level.
        mode = "adopt"
        normalized_services = [_normalize_v1_adopt(raw)]
        drop = ("service", "dockerfile", "dockerfile_context", "port", "stages")
    else:
        if services is None:
            services = []
        if not isinstance(services, list):
            raise ConfigError("'services' must be a list of objects")
        if version == SCHEMA_VERSION:
            mode = raw.get("mode", "scaffold")
            if mode not in ("scaffold", "adopt"):
                raise ConfigError(
                    f"unknown mode {mode!r} (expected 'scaffold' or 'adopt')"
                )
            normalized_services = [
                _normalize_service_v2(s, mode) for s in services
            ]
            drop = ()
        else:
            # v1 scaffold shape.
            mode = "scaffold"
            normalized_services = [
                _normalize_v1_scaffold_service(s) for s in services
            ]
            drop = ("service", "dockerfile", "dockerfile_context", "stages")

    out = dict(raw)
    for key in drop:
        out.pop(key, None)
    out["version"] = SCHEMA_VERSION
    out["mode"] = mode
    out["services"] = normalized_services
    out.setdefault("inject_ide", False)
    out.setdefault("compose_files", list(DEFAULT_COMPOSE_FILES))
    out.setdefault("mcp", copy.deepcopy(DEFAULT_MCP_CONFIG))
    return out


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _config_path(project_root):
    return os.path.join(project_root, "devbox.json")


def _target_mode(path):
    # mkstemp creates 0600 files; keep the existing file's mode, or use the
    # regular umask-based default for new files.
    try:
        return os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def _atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    mode = _target_mode(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".devbox-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config(project_root: str) -> dict | None:
    """Load and normalize ``project_root/devbox.json``.

    Returns ``None`` when the file does not exist. Raises :class:`ConfigError`
    for invalid JSON or a malformed shape. Host-relative service paths are
    normalized to posix paths relative to ``project_root``.
    """
    path = _config_path(project_root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    cfg = normalize_config(raw)
    for svc in cfg["services"]:
        if svc.get("source") is not None:
            svc["source"] = normalize_rel_path(svc["source"], project_root)
        if svc.get("context") is not None:
            svc["context"] = normalize_rel_path(svc["context"], project_root)
    return cfg


def save_config(project_root: str, cfg: dict) -> str:
    """Atomically write ``cfg`` to ``project_root/devbox.json`` (indent 2).

    Returns the written path. The previous file is replaced in one step and the
    output always ends with a trailing newline.
    """
    path = _config_path(project_root)
    _atomic_write(path, json.dumps(cfg, indent=2) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Service helpers
# --------------------------------------------------------------------------- #
def get_service(cfg: dict, name: str) -> dict | None:
    """Return the service dict named ``name`` or ``None``."""
    for svc in cfg.get("services", []):
        if svc.get("name") == name:
            return svc
    return None


def upsert_service(cfg: dict, svc: dict) -> None:
    """Replace the service with the same name, or append ``svc``."""
    services = cfg.setdefault("services", [])
    for i, existing in enumerate(services):
        if existing.get("name") == svc.get("name"):
            services[i] = svc
            return
    services.append(svc)


def remove_service(cfg: dict, name: str) -> bool:
    """Remove the named service. Returns ``True`` if it was present."""
    services = cfg.get("services", [])
    for i, existing in enumerate(services):
        if existing.get("name") == name:
            del services[i]
            return True
    return False


def find_compose_file(project_root: str) -> str | None:
    """Return the path of the first existing compose candidate, or ``None``."""
    for candidate in COMPOSE_CANDIDATES:
        path = os.path.join(project_root, candidate)
        if os.path.isfile(path):
            return path
    return None


# --------------------------------------------------------------------------- #
# Marker-block and file helpers
# --------------------------------------------------------------------------- #
def upsert_marked_block(path: str, body: str, begin: str, end: str, create: bool = True) -> str:
    """Insert/replace a marker-delimited block in ``path``.

    Returns ``"created"``, ``"updated"``, ``"unchanged"`` or ``"skipped"`` (file
    missing and ``create=False``). Only the text between the markers is
    replaced; when the file exists without markers the block is appended,
    separated by one blank line. Text outside the markers is preserved
    byte-for-byte.
    """
    body = body.strip("\n")
    block = f"{begin}\n{body}\n{end}"

    if not os.path.exists(path):
        if not create:
            return "skipped"
        _atomic_write(path, block + "\n")
        return "created"

    with open(path, encoding="utf-8") as f:
        content = f.read()

    if begin in content and end in content and content.index(begin) <= content.index(end):
        start = content.index(begin)
        stop = content.index(end) + len(end)
        new_content = content[:start] + block + content[stop:]
        if new_content == content:
            return "unchanged"
        _atomic_write(path, new_content)
        return "updated"

    separator = content
    if separator and not separator.endswith("\n"):
        separator += "\n"
    _atomic_write(path, separator + "\n" + block + "\n")
    return "created"


def architecture_target(project_root: str) -> str:
    """Return the path DevBox may write its architecture block to.

    ``ARCHITECTURE.md`` if it is absent or already contains DevBox's marker;
    otherwise ``DEVBOX.md`` so a user-authored file is never touched.
    """
    path = os.path.join(project_root, "ARCHITECTURE.md")
    if not os.path.exists(path):
        return path
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return os.path.join(project_root, "DEVBOX.md")
    if MD_BEGIN in content:
        return path
    return os.path.join(project_root, "DEVBOX.md")


def render_architecture(cfg: dict) -> str:
    """Render the DevBox architecture body (no markers).

    Describes the actual host->container mapping from ``cfg`` and never claims a
    fixed ``src/`` layout, so adopted projects render their real paths.
    """
    services = cfg.get("services") or []
    compose_files = cfg.get("compose_files") or DEFAULT_COMPOSE_FILES

    lines = [
        "# Project Architecture",
        "",
        "This project runs on the **DevBox Platform**. The section between the "
        "`devbox` markers is generated by DevBox; everything outside them is "
        "yours to edit.",
        "",
        "## Services",
        "",
        "| Service | Host folder | Container path | Port | Owner |",
        "| --- | --- | --- | --- | --- |",
    ]
    if services:
        for svc in services:
            source = svc.get("source")
            workdir = svc.get("workdir")
            port = svc.get("port")
            owner = "managed" if svc.get("managed") else "user"
            lines.append(
                f"| {svc.get('name')} | `{source if source is not None else '(image only)'}` "
                f"| `{workdir if workdir is not None else '(n/a)'}` "
                f"| {port if port is not None else '-'} | {owner} |"
            )
    else:
        lines.append("| _(no services yet)_ |  |  |  |  |")

    lines += [
        "",
        "Each service mounts the host folder above into its container path; "
        "DevBox makes no assumption about a particular directory layout.",
        "",
        "## How to develop",
        "",
        "1. **Load the CLI tools:** `source build/envsetup.sh`",
        "2. **Set up AI tooling (optional):** `devbox-ai-setup`",
        "3. **Start the environment:** `devbox-up`",
        "",
        "Run a command inside a service container with:",
        "",
    ]
    if services:
        for svc in services:
            lines.append(f"* `devbox-run {svc.get('name')} <command>`")
    else:
        lines.append("* `devbox-run <service> <command>`")

    lines += [
        "",
        "### The Docker Compose layers",
        "",
        "DevBox composes the following files (in order):",
        "",
    ]
    for i, compose_file in enumerate(compose_files, 1):
        lines.append(f"{i}. `{compose_file}`")
    lines += [
        "",
        "Edit the dev override for local testing and only touch the base compose "
        "file when you add a production service.",
        "",
    ]
    return "\n".join(lines)


def write_architecture(project_root: str, cfg: dict) -> str:
    """Upsert the DevBox architecture block and return the path written."""
    target = architecture_target(project_root)
    body = render_architecture(cfg)
    upsert_marked_block(target, body, MD_BEGIN, MD_END)
    return target


def write_file_safely(path: str, content: str, overwrite: bool = False, backup: bool = True) -> str:
    """Write ``content`` to ``path`` defensively.

    Returns ``"created"``, ``"skipped"`` (exists and ``overwrite`` is false),
    ``"overwritten"`` (writes ``path + ".bak"`` first when ``backup``) or
    ``"unchanged"`` (exists with identical content).
    """
    if not os.path.exists(path):
        _atomic_write(path, content)
        return "created"
    with open(path, encoding="utf-8") as f:
        existing = f.read()
    if not overwrite:
        return "skipped"
    if existing == content:
        return "unchanged"
    if backup:
        _atomic_write(path + ".bak", existing)
    _atomic_write(path, content)
    return "overwritten"
