#!/usr/bin/env python3
"""devbox-adopt: layout-aware brownfield adoption wizard.

Scans an existing project (docker-compose file, Dockerfiles, runtimes) and
generates only the DevBox-owned pieces it is missing:

  * ``devbox.json``                -- project config (v2 schema)
  * ``docker-compose.yml``         -- only when the project has no compose file
  * ``docker-compose.dev.yml``     -- dev overrides: mounts, target, IDE
  * ``ARCHITECTURE.md``/``DEVBOX.md`` -- marker-scoped architecture block
  * ``.gitignore``                 -- marker-scoped DevBox ignore block
  * ``ai/``                        -- empty, for project AI overrides

The container layout follows the image layout: each service mounts exactly the
host folder its Dockerfile context comes from, at the WORKDIR the Dockerfile
declares (or an existing compose bind target), never a blind ``.:/app``.

Ownership: adoption is non-destructive. User files (compose file, Dockerfiles,
sources) are never moved, renamed, deleted or edited. Files that must contain a
DevBox section are only ever changed through marker blocks, and only with
consent (interactive prompt, or ``--yes``).

Usage::

    adopt.py <project_root> [--dry-run] [--yes]
             [--service name:source[:workdir]]... [--inject-ide]

Runs natively on the host (Docker is normally available but never required:
every ``docker`` call has a pure-Python fallback).
"""

import difflib
import json
import os
import posixpath
import re
import subprocess
import sys

from devbox_config import (
    HASH_BEGIN,
    HASH_END,
    MD_BEGIN,
    MD_END,
    ConfigError,
    architecture_target,
    find_compose_file,
    load_config,
    make_service,
    new_config,
    normalize_rel_path,
    render_architecture,
    sanitize_service_name,
    save_config,
    upsert_marked_block,
    write_file_safely,
)
from scaffold import load_manifest

MAX_DOCKERFILE_DEPTH = 4

SKIP_DIRS = {
    ".git",
    "node_modules",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    ".opencode",
    ".nvim",
    "vendor",
    "target",
    ".next",
}

# Firestore/Auth/Functions/Emulator-UI/Hosting, kept from the original adopt.
FIREBASE_EMULATOR_PORTS = [
    "9099:9099",
    "8080:8080",
    "5001:5001",
    "4001:4001",
    "6001:6001",
]

# Ordered: the first matching marker in a build context decides the runtime.
RUNTIME_MARKERS = (
    ("package.json", "node"),
    ("requirements.txt", "python"),
    ("pyproject.toml", "python"),
    ("Pipfile", "python"),
    ("go.mod", "go"),
    ("Gemfile", "ruby"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
)

IDE_INJECTION_MARKER = "@DEVBOX_IDE_INJECTION_POINT@"

USAGE = (
    "usage: adopt.py <project_root> [--dry-run] [--yes]\n"
    "                 [--service name:source[:workdir]]... [--inject-ide]"
)

FROM_RE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)


class UsageError(Exception):
    """Raised for malformed command-line input (maps to exit code 2)."""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv):
    """Parse ``argv`` into an options dict. Raises :class:`UsageError`."""
    project_root = None
    dry_run = False
    yes = False
    inject_ide = False
    raw_services = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--dry-run":
            dry_run = True
        elif arg == "--yes":
            yes = True
        elif arg == "--inject-ide":
            inject_ide = True
        elif arg == "--service":
            i += 1
            if i >= len(argv):
                raise UsageError("--service requires a value name:source[:workdir]")
            raw_services.append(argv[i])
        elif arg.startswith("--service="):
            raw_services.append(arg.split("=", 1)[1])
        elif arg.startswith("-") and arg != "-":
            raise UsageError(f"unknown option {arg!r}")
        else:
            if project_root is not None:
                raise UsageError(f"unexpected argument {arg!r}")
            project_root = arg
        i += 1

    project_root = os.path.abspath(project_root or os.getcwd())
    services = [parse_service_spec(spec, project_root) for spec in raw_services]
    return {
        "project_root": project_root,
        "dry_run": dry_run,
        "yes": yes,
        "inject_ide": inject_ide,
        "services": services,
    }


def parse_service_spec(spec, project_root):
    """Parse ``name:source[:workdir]`` and validate every part."""
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise UsageError(
            f"invalid --service {spec!r}: expected name:source[:workdir]"
        )
    try:
        name = sanitize_service_name(parts[0])
        source = normalize_rel_path(parts[1], project_root)
    except ConfigError as exc:
        raise UsageError(f"invalid --service {spec!r}: {exc}") from exc
    workdir = parts[2] if len(parts) == 3 else None
    if workdir is not None and not workdir.startswith("/"):
        raise UsageError(
            f"invalid --service {spec!r}: workdir must be an absolute container path"
        )
    return {"name": name, "source": source, "workdir": workdir}


# --------------------------------------------------------------------------- #
# Interactive helpers
# --------------------------------------------------------------------------- #
def _read(prompt_text):
    """Read a line, treating EOF (closed/partial stdin) as "no answer"."""
    try:
        return input(prompt_text)
    except EOFError:
        print()
        return None


def prompt(msg, default=None):
    if default is not None:
        result = _read(f"{msg} [{default}]: ")
        if result is None:
            return default
        result = result.strip()
        return result if result else default
    result = _read(f"{msg}: ")
    return "" if result is None else result.strip()


def confirm(msg, default=True):
    suffix = " [Y/n]" if default else " [y/N]"
    result = _read(f"{msg}{suffix}: ")
    if result is None:
        return default
    result = result.strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def prompt_int(msg, default=None):
    """Prompt for a whole number, re-asking on invalid input (never crashes)."""
    label = "none" if default is None else str(default)
    while True:
        raw = _read(f"{msg} [{label}]: ")
        if raw is None:
            return default
        raw = raw.strip()
        if not raw:
            return default
        if raw.lower() in ("none", "-", "null"):
            return None
        try:
            return int(raw)
        except ValueError:
            print(f"  {raw!r} is not a whole number; please try again.")


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def _build_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_dockerfile(filename):
    return (
        filename == "Dockerfile"
        or filename.startswith("Dockerfile.")
        or filename.endswith(".Dockerfile")
    )


def _to_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _parse_port_string(value):
    text = value.split("/", 1)[0].strip()
    if not text:
        return None
    target = text.split(":")[-1]
    if "-" in target:
        target = target.split("-", 1)[0]
    return _to_int(target)


def default_port(runtime, manifest):
    if not runtime:
        return None
    entry = (manifest.get("runtimes") or {}).get(runtime)
    if entry:
        return entry.get("default_port")
    return None


def home_dir(runtime, manifest):
    entry = (manifest.get("runtimes") or {}).get(runtime) if runtime else None
    if entry and entry.get("home_dir"):
        return entry["home_dir"]
    return "/root"


def find_env_file(project_root):
    for name in (".env", ".env.local", ".env.example"):
        path = os.path.join(project_root, name)
        if os.path.isfile(path):
            return path
    return None


def read_env_keys(env_path):
    """Read variable names from a .env file without parsing values."""
    keys = []
    if not env_path:
        return keys
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    keys.append(line.split("=", 1)[0].strip())
    except OSError:
        pass
    return keys


def detect_runtime(directory):
    if not os.path.isdir(directory):
        return None
    for marker, runtime in RUNTIME_MARKERS:
        if os.path.isfile(os.path.join(directory, marker)):
            return runtime
    return None


# --------------------------------------------------------------------------- #
# Dockerfile analysis (stage aware)
# --------------------------------------------------------------------------- #
def parse_dockerfile(path):
    """Parse the Dockerfile at ``path`` (see :func:`parse_dockerfile_text`)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {
            "path": path,
            "exists": False,
            "stages": [],
            "dev_target": None,
            "warnings": [],
        }
    analysis = parse_dockerfile_text(text, path)
    analysis["exists"] = True
    return analysis


def parse_dockerfile_text(text, path="Dockerfile"):
    """Parse Dockerfile ``text`` into stages with resolved WORKDIR / EXPOSE.

    Returns::

        {
          "path": str,
          "exists": bool,
          "stages": [{"name": str|None, "workdir": str|None, "expose": [int]}],
          "dev_target": str|None,
          "warnings": [str],
        }

    ``WORKDIR`` follows ``FROM <stage>`` inheritance, joins relative paths and
    falls back to ``/app`` (with a warning) when it contains a variable.
    """
    analysis = {
        "path": path,
        "exists": True,
        "stages": [],
        "dev_target": None,
        "warnings": [],
    }

    stages = analysis["stages"]
    index = {}
    current = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        match = FROM_RE.match(line)
        if match:
            image, alias = match.group(1), match.group(2)
            base_workdir = None
            base_expose = []
            prev = index.get(image.lower())
            if prev is not None:
                base_workdir = stages[prev]["workdir"]
                base_expose = list(stages[prev]["expose"])
            current = {"name": alias, "workdir": base_workdir, "expose": base_expose}
            stages.append(current)
            if alias:
                index[alias.lower()] = len(stages) - 1
            continue

        if current is None:
            continue

        upper = line.upper()
        if upper.startswith("WORKDIR "):
            value = line.split(None, 1)[1].strip().strip('"').strip("'")
            if "$" in value:
                current["workdir"] = "/app"
                analysis["warnings"].append(
                    f"{os.path.basename(path)}: WORKDIR {value!r} contains a variable; "
                    "assuming /app"
                )
            elif value.startswith("/"):
                current["workdir"] = posixpath.normpath(value)
            else:
                base = current["workdir"] or "/"
                current["workdir"] = posixpath.normpath(posixpath.join(base, value))
        elif upper.startswith("EXPOSE "):
            for token in line.split()[1:]:
                port = _to_int(token.split("/", 1)[0])
                if port is not None:
                    current["expose"].append(port)

    for stage in stages:
        if stage["name"] and stage["name"].lower() in ("dev", "development"):
            analysis["dev_target"] = stage["name"]
            break
    return analysis


def dockerfile_dev_target(analysis):
    return analysis.get("dev_target")


def _selected_stage(analysis, target=None):
    stages = analysis.get("stages") or []
    if not stages:
        return None
    key = (target or analysis.get("dev_target") or "").lower()
    if key:
        for stage in stages:
            if stage["name"] and stage["name"].lower() == key:
                return stage
    return stages[-1]


def dockerfile_workdir(analysis, target=None):
    stage = _selected_stage(analysis, target)
    return stage["workdir"] if stage else None


def dockerfile_expose(analysis, target=None):
    stage = _selected_stage(analysis, target)
    if stage and stage["expose"]:
        return stage["expose"][0]
    return None


# --------------------------------------------------------------------------- #
# Compose discovery
# --------------------------------------------------------------------------- #
def _docker_compose_json(project_root, compose_path, run=None):
    """Return ``docker compose config --format json`` output, or ``None``."""
    if run is None:
        run = subprocess.run
    try:
        result = run(
            ["docker", "compose", "-f", compose_path, "config", "--format", "json"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(getattr(result, "stdout", "") or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return None
    return data


def discover_compose(project_root, compose_path, run=None):
    """Return normalized compose services, using Docker then a fallback parser.

    Each entry is a dict with ``name``, ``build_context``, ``build_dockerfile``,
    ``build_target``, ``image``, ``ports`` (target container ports), 
    ``working_dir``, ``binds`` and ``mappable``.
    """
    data = _docker_compose_json(project_root, compose_path, run)
    if data is not None:
        return _normalize_compose_services(data.get("services", {}), project_root)

    try:
        with open(compose_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    return _normalize_compose_services(parse_compose_fallback(text), project_root)


def _normalize_compose_services(services, project_root):
    result = []
    for name, raw in services.items():
        entry = _normalize_compose_service(name, raw, project_root)
        if entry is not None:
            result.append(entry)
    return result


def _normalize_compose_service(name, raw, project_root):
    if not isinstance(raw, dict):
        raw = {}
    try:
        clean_name = sanitize_service_name(name)
    except ConfigError:
        return None

    entry = {
        "name": clean_name,
        "build_context": None,
        "build_dockerfile": "Dockerfile",
        "build_target": None,
        "image": None,
        "ports": [],
        "working_dir": None,
        "binds": [],
        "mappable": True,
        "warnings": [],
    }

    build = raw.get("build")
    if isinstance(build, str) and build.strip():
        entry["build_context"] = build.strip()
    elif isinstance(build, dict):
        context = build.get("context")
        entry["build_context"] = str(context) if context else "."
        dockerfile = build.get("dockerfile")
        if dockerfile:
            entry["build_dockerfile"] = str(dockerfile)
        target = build.get("target")
        if target:
            entry["build_target"] = str(target)

    image = raw.get("image")
    if isinstance(image, str) and image.strip():
        entry["image"] = image.strip()

    working_dir = raw.get("working_dir")
    if isinstance(working_dir, str) and working_dir.strip():
        entry["working_dir"] = working_dir.strip()

    entry["ports"] = _parse_ports(raw.get("ports"))
    entry["binds"] = _parse_binds(raw.get("volumes"), project_root)

    if entry["build_context"] is not None:
        try:
            entry["build_context"] = normalize_rel_path(
                entry["build_context"], project_root
            )
        except ConfigError:
            entry["mappable"] = False
            entry["warnings"].append(
                f"build context escapes the project root ({entry['build_context']!r})"
            )
            entry["build_context"] = None

    # A docker-resolved dockerfile may be absolute; the schema wants it relative
    # to the build context.
    dockerfile = entry["build_dockerfile"]
    if os.path.isabs(dockerfile):
        if entry["build_context"] is not None:
            context_abs = os.path.join(project_root, entry["build_context"])
            try:
                dockerfile = os.path.relpath(dockerfile, context_abs)
            except ValueError:
                dockerfile = os.path.basename(dockerfile)
        else:
            dockerfile = os.path.basename(dockerfile)
        entry["build_dockerfile"] = dockerfile

    return entry


def _parse_ports(value):
    ports = []
    if not isinstance(value, list):
        return ports
    for item in value:
        if isinstance(item, dict):
            port = _to_int(item.get("target"))
        elif isinstance(item, (int, str)):
            port = _parse_port_string(str(item))
        else:
            port = None
        if port:
            ports.append(port)
    return ports


def _parse_volume_string(item):
    parts = item.split(":")
    if len(parts) < 2:
        return None
    source = parts[0].strip()
    target = parts[1].strip()
    if not source or not target:
        return None
    return source, target


def _parse_binds(value, project_root):
    binds = []
    if not isinstance(value, list):
        return binds
    for item in value:
        source = target = None
        if isinstance(item, dict):
            if item.get("type") not in (None, "bind"):
                continue
            source = item.get("source")
            target = item.get("target")
        elif isinstance(item, str):
            parsed = _parse_volume_string(item.strip())
            if parsed is None:
                continue
            source, target = parsed
        if not source or not target:
            continue
        # Only bind mounts (paths), never named volumes.
        if not (source.startswith(".") or source.startswith("/") or source.startswith("~")):
            continue
        try:
            rel = normalize_rel_path(source, project_root)
        except ConfigError:
            rel = None
        binds.append({"source": rel, "target": target, "raw_source": source})
    return binds


# --------------------------------------------------------------------------- #
# Fallback compose parser (indentation based, stdlib only)
# --------------------------------------------------------------------------- #
def _strip_comment(line):
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    if value == "{}":
        return {}
    if value == "[]":
        return []
    return value


def _split_pair(text):
    if text[:1] in ("'", '"'):
        quote = text[0]
        end = text.find(quote, 1)
        if end == -1:
            return None
        rest = text[end + 1:].strip()
        if not rest.startswith(":"):
            return None
        return text[1:end], rest[1:].strip()
    if ": " in text:
        key, _, val = text.partition(": ")
        return key.strip(), val.strip()
    if text.endswith(":"):
        return text[:-1].strip(), ""
    return None


class _ComposeSubsetParser:
    """Tiny indentation-based parser for the compose subset adopt needs."""

    def __init__(self, lines):
        self.lines = lines  # list of (indent, text)
        self.pos = 0

    def parse(self):
        if not self.lines:
            return {}
        return self._parse_map(self.lines[0][0])

    def _parse_block(self, indent):
        if self.pos >= len(self.lines):
            return {}
        if self.lines[self.pos][1].startswith("- "):
            return self._parse_list(self.lines[self.pos][0])
        return self._parse_map(indent)

    def _parse_map(self, indent):
        result = {}
        while self.pos < len(self.lines):
            ind, text = self.lines[self.pos]
            if ind < indent:
                break
            if ind > indent:
                self.pos += 1
                continue
            if text.startswith("- "):
                break
            pair = _split_pair(text)
            if pair is None:
                self.pos += 1
                continue
            key, val = pair
            self.pos += 1
            if val != "":
                result[key] = _scalar(val)
            else:
                nxt = self.lines[self.pos] if self.pos < len(self.lines) else None
                if nxt and (
                    nxt[0] > ind or (nxt[0] == ind and nxt[1].startswith("- "))
                ):
                    result[key] = self._parse_block(nxt[0])
                else:
                    result[key] = {}
        return result

    def _parse_list(self, indent):
        items = []
        while self.pos < len(self.lines):
            ind, text = self.lines[self.pos]
            if ind != indent or not text.startswith("- "):
                break
            rest = text[2:].strip()
            self.pos += 1
            if not rest:
                nxt = self.lines[self.pos] if self.pos < len(self.lines) else None
                if nxt and nxt[0] > indent:
                    items.append(self._parse_block(nxt[0]))
                else:
                    items.append(None)
                continue
            pair = _split_pair(rest)
            if pair is None:
                items.append(_scalar(rest))
                continue
            item = {}
            self._assign_pair(item, pair, indent + 2)
            while (
                self.pos < len(self.lines)
                and self.lines[self.pos][0] > indent
                and not self.lines[self.pos][1].startswith("- ")
            ):
                ind2, text2 = self.lines[self.pos]
                pair2 = _split_pair(text2)
                self.pos += 1
                if pair2 is not None:
                    self._assign_pair(item, pair2, ind2)
            items.append(item)
        return items

    def _assign_pair(self, mapping, pair, key_indent):
        key, val = pair
        if val != "":
            mapping[key] = _scalar(val)
            return
        nxt = self.lines[self.pos] if self.pos < len(self.lines) else None
        if nxt and (
            nxt[0] > key_indent or (nxt[0] == key_indent and nxt[1].startswith("- "))
        ):
            mapping[key] = self._parse_block(nxt[0])
        else:
            mapping[key] = {}


def parse_compose_fallback(text):
    lines = []
    for raw in text.splitlines():
        cleaned = _strip_comment(raw).rstrip()
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" \t"))
        lines.append((indent, cleaned.strip()))
    if not lines:
        return {}
    document = _ComposeSubsetParser(lines).parse()
    services = document.get("services")
    return services if isinstance(services, dict) else {}


# --------------------------------------------------------------------------- #
# Discovery -> plan
# --------------------------------------------------------------------------- #
def find_dockerfile_candidates(project_root):
    """Return ``(context, dockerfile_name)`` pairs, depth limited and pruned."""
    root = os.path.abspath(project_root)
    candidates = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= MAX_DOCKERFILE_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        context = "." if rel == "." else rel.replace(os.sep, "/")
        for filename in sorted(filenames):
            if _is_dockerfile(filename):
                candidates.append((context, filename))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates


def _root_source_warning(source, workdir):
    if source == ".":
        return (
            "source is the project root; the whole project (including build/ and "
            ".env) will be mounted"
        )
    return None


def _plan_build_entry(name, source, context, dockerfile, compose_target,
                      compose_working_dir, compose_ports, binds, project_root,
                      manifest, dockerfile_content=None):
    context_abs = project_root if context == "." else os.path.join(project_root, context)
    dockerfile_path = os.path.join(context_abs, dockerfile)
    if dockerfile_content is not None:
        # Planned-but-not-yet-written Dockerfile (e.g. generated from a template).
        analysis = parse_dockerfile_text(dockerfile_content, dockerfile)
    else:
        analysis = parse_dockerfile(dockerfile_path)
    runtime = detect_runtime(context_abs)

    dev_target = compose_target or dockerfile_dev_target(analysis)
    workdir = (
        compose_working_dir
        or dockerfile_workdir(analysis, compose_target)
        or "/app"
    )

    reused_bind = None
    for bind in binds:
        if bind["target"] == workdir and bind["source"]:
            reused_bind = bind
            break
    if reused_bind is not None:
        source = reused_bind["source"]
        workdir = reused_bind["target"]

    port = compose_ports[0] if compose_ports else None
    if port is None:
        port = dockerfile_expose(analysis, compose_target)
    if port is None:
        port = default_port(runtime, manifest)

    warnings = list(analysis["warnings"])
    root_warning = _root_source_warning(source, workdir)
    if root_warning:
        warnings.append(root_warning)

    service = make_service(
        name,
        source,
        workdir=workdir,
        context=context,
        dockerfile=dockerfile,
        target=dev_target,
        port=port,
        runtime=runtime,
        managed=False,
    )
    return {
        "service": service,
        "kind": "build",
        "reused_bind": reused_bind,
        "warnings": warnings,
        "analysis": analysis,
    }


def plan_compose_services(compose_services, project_root, manifest):
    plan = []
    for entry in compose_services:
        if entry["build_context"] is None:
            port = entry["ports"][0] if entry["ports"] else None
            plan.append(
                {
                    "service": make_service(entry["name"], None, port=port, managed=False),
                    "kind": "image",
                    "reused_bind": None,
                    "warnings": list(entry["warnings"]),
                    "analysis": None,
                }
            )
            continue
        plan.append(
            _plan_build_entry(
                entry["name"],
                entry["build_context"],
                entry["build_context"],
                entry["build_dockerfile"] or "Dockerfile",
                entry["build_target"],
                entry["working_dir"],
                entry["ports"],
                entry["binds"],
                project_root,
                manifest,
            )
        )
        plan[-1]["warnings"] = list(entry["warnings"]) + plan[-1]["warnings"]
    return plan


def plan_dockerfile_services(candidates, project_root, manifest):
    plan = []
    used = {}
    for context, dockerfile in candidates:
        base = project_root if context == "." else os.path.join(project_root, context)
        try:
            name = sanitize_service_name(os.path.basename(os.path.abspath(base)))
        except ConfigError:
            continue
        if name in used:
            used[name] += 1
            name = f"{name}-{used[name]}"
        else:
            used[name] = 0
        plan.append(
            _plan_build_entry(
                name,
                context,
                context,
                dockerfile,
                None,
                None,
                [],
                [],
                project_root,
                manifest,
            )
        )
    return plan


def plan_from_source(project_root, name, source, workdir, manifest):
    """Build a plan entry for an explicit ``--service`` mapping."""
    source_abs = project_root if source == "." else os.path.join(project_root, source)
    dockerfile = "Dockerfile"
    if not os.path.isfile(os.path.join(source_abs, dockerfile)) and os.path.isdir(source_abs):
        for filename in sorted(os.listdir(source_abs)):
            if _is_dockerfile(filename) and os.path.isfile(os.path.join(source_abs, filename)):
                dockerfile = filename
                break
    return _plan_build_entry(
        name,
        source,
        source,
        dockerfile,
        None,
        workdir,
        [],
        [],
        project_root,
        manifest,
    )


def apply_service_overrides(project_root, plan, overrides, manifest):
    for override in overrides:
        entry = plan_from_source(
            project_root,
            override["name"],
            override["source"],
            override["workdir"],
            manifest,
        )
        for i, existing in enumerate(plan):
            if existing["service"]["name"] == override["name"]:
                plan[i] = entry
                break
        else:
            plan.append(entry)


def render_root_dockerfile(manifest, runtime, build_dir=None):
    """Return the runtime's Dockerfile template with IDE markers stripped.

    Returns ``None`` when the runtime has no template. Pure: nothing is written.
    """
    entry = (manifest.get("runtimes") or {}).get(runtime)
    if not entry:
        return None
    if build_dir is None:
        build_dir = _build_dir()
    template = os.path.join(build_dir, "templates", "dockerfiles", entry["dockerfile"])
    try:
        with open(template, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    return "\n".join(
        line for line in content.split("\n") if IDE_INJECTION_MARKER not in line
    )


def pending_dockerfiles(plan, project_root):
    """Yield ``(path, runtime)`` for root Dockerfiles the plan wants to create."""
    for entry in plan:
        generated = entry.get("generate_dockerfile")
        if generated:
            yield os.path.join(project_root, "Dockerfile"), generated["runtime"]


def discover_plan(project_root, compose_path, manifest, opts, interactive):
    """Run discovery and return ``(plan, generated_files)`` or ``(None, [])``."""
    build_dir = _build_dir()
    generated_files = []
    plan = []

    if compose_path:
        plan = plan_compose_services(
            discover_compose(project_root, compose_path), project_root, manifest
        )

    if not plan:
        candidates = find_dockerfile_candidates(project_root)
        if candidates:
            plan = plan_dockerfile_services(candidates, project_root, manifest)

    if not plan:
        runtime = detect_runtime(project_root)
        if runtime is None:
            print(
                "\nNo Dockerfile or compose file was found, and the project runtime "
                "could not be detected."
            )
            print(
                "Add a Dockerfile or docker-compose.yml first, or run "
                "'devbox-init' to scaffold a brand new project."
            )
            return None, []
        if runtime not in (manifest.get("runtimes") or {}):
            print(f"\nDetected runtime {runtime!r}, but DevBox ships no Dockerfile "
                  "template for it.")
            print("Add a Dockerfile manually, or run 'devbox-init' for a scaffolded "
                  "project.")
            return None, []
        create = True
        if interactive:
            create = confirm(
                f"No Dockerfile found. Create one at the project root from the "
                f"{runtime} template?",
                default=True,
            )
        if not create:
            print("No services to adopt, aborting.")
            return None, []
        content = render_root_dockerfile(manifest, runtime, build_dir)
        if content is None:
            print("Could not locate the runtime Dockerfile template.")
            return None, []
        generated_files.append("Dockerfile")
        try:
            name = sanitize_service_name(os.path.basename(os.path.abspath(project_root)))
        except ConfigError:
            name = "app"
        entry = _plan_build_entry(
            name,
            ".",
            ".",
            "Dockerfile",
            None,
            None,
            [],
            [],
            project_root,
            manifest,
            dockerfile_content=content,
        )
        entry["generate_dockerfile"] = {"runtime": runtime}
        plan = [entry]

    apply_service_overrides(project_root, plan, opts["services"], manifest)

    if interactive:
        for entry in plan:
            service = entry["service"]
            if service.get("source") != ".":
                continue
            keep_root = confirm(
                f"Service {service['name']!r} mounts the whole project (.) at "
                f"{service.get('workdir') or '/app'}. Continue?",
                default=True,
            )
            if keep_root:
                continue
            new_source = prompt("Source folder", default=".")
            try:
                new_source = normalize_rel_path(new_source, project_root)
            except ConfigError as exc:
                print(f"Invalid source ({exc}); keeping the project root.")
                continue
            entry.update(
                plan_from_source(
                    project_root,
                    service["name"],
                    new_source,
                    service.get("workdir"),
                    manifest,
                )
            )

    if interactive:
        for entry in plan:
            service = entry["service"]
            if entry["kind"] == "build" and service.get("port") is None:
                service["port"] = prompt_int(
                    f"Port for {service['name']} (blank to skip)"
                )

    return plan, generated_files


def collect_extras(project_root, interactive, opts):
    env_keys = read_env_keys(find_env_file(project_root))
    has_firebase = os.path.isfile(os.path.join(project_root, "firebase.json"))
    functions_dir = "functions" if os.path.isdir(os.path.join(project_root, "functions")) else None

    default_preserved = list(env_keys)
    pending_inject_ide = opts["inject_ide"]

    if interactive:
        pending_inject_ide = opts["inject_ide"] or confirm(
            "Inject containerized IDE (Neovim/Lazygit)?", default=False
        )
        has_firebase = confirm("Does this project use Firebase?", default=has_firebase)

    if has_firebase:
        for name in ("GEMINI_API_KEY", "FIREBASE_PROJECT_ID"):
            if name not in default_preserved:
                default_preserved.append(name)

    if interactive:
        if default_preserved:
            answer = prompt(
                "Env vars to preserve (comma-separated)",
                default=", ".join(default_preserved),
            )
        else:
            answer = prompt("Env vars to preserve (comma-separated)", default="none")
        preserved = [
            item.strip()
            for item in answer.split(",")
            if item.strip() and item.strip() != "none"
        ]
    else:
        preserved = list(default_preserved)

    return {
        "inject_ide": pending_inject_ide,
        "has_firebase": has_firebase,
        "functions_dir": functions_dir,
        "preserve_env": preserved,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _ctx_path(context):
    return "." if context == "." else f"./{context}"


def _volume_path(source):
    return "." if source == "." else f"./{source}"


def render_services_table(plan):
    headers = ["SERVICE", "SOURCE", "WORKDIR", "PORT", "TARGET", "RUNTIME", "OWNER"]
    rows = []
    for entry in plan:
        service = entry["service"]
        source = service.get("source")
        rows.append(
            [
                service.get("name") or "",
                source if source is not None else "(image only)",
                service.get("workdir") if service.get("workdir") is not None else "-",
                str(service["port"]) if service.get("port") is not None else "-",
                service.get("target") or "-",
                service.get("runtime") or "-",
                "managed" if service.get("managed") else "user",
            ]
        )
    if not rows:
        return "(no services detected)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append(
            "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def render_plan(plan):
    lines = ["", "Detected services:", "", render_services_table(plan)]
    warnings = []
    for entry in plan:
        for warning in entry.get("warnings", []):
            warnings.append(f"  ! [{entry['service']['name']}] {warning}")
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(warnings)
    return "\n".join(lines)


def render_compose_base(cfg):
    lines = ["services:"]
    for service in cfg.get("services", []):
        source = service.get("source")
        if source is None:
            continue
        lines.append(f"  {service['name']}:")
        lines.append("    build:")
        lines.append(f"      context: {_ctx_path(service.get('context') or source)}")
        dockerfile = service.get("dockerfile")
        if dockerfile and dockerfile != "Dockerfile":
            lines.append(f"      dockerfile: {dockerfile}")
        if service.get("port"):
            lines.append("    ports:")
            lines.append(f'      - "{service["port"]}:{service["port"]}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_compose_dev(cfg, manifest, extras, reused_binds):
    has_firebase = bool(extras.get("has_firebase"))
    functions_dir = extras.get("functions_dir")
    inject_ide = bool(extras.get("inject_ide"))
    reused_binds = reused_binds or set()

    lines = ["services:"]
    first_build = None
    for service in cfg.get("services", []):
        source = service.get("source")
        if source is None:
            continue
        name = service["name"]
        if first_build is None:
            first_build = name
        workdir = service.get("workdir") or "/app"

        lines.append(f"  {name}:")
        if service.get("target"):
            lines.append("    build:")
            lines.append(f"      target: {service['target']}")
        lines.append('    user: "${DEVBOX_UID:-1000}:${DEVBOX_GID:-1000}"')
        if has_firebase and name == first_build:
            lines.append("    ports:")
            for port in FIREBASE_EMULATOR_PORTS:
                lines.append(f'      - "{port}"')
        lines.append("    volumes:")
        if name not in reused_binds:
            lines.append(f"      - {_volume_path(source)}:{workdir}")
        runtime = service.get("runtime")
        if runtime in ("node", "node-debian"):
            lines.append(f"      - {workdir}/node_modules")
            if has_firebase and functions_dir:
                lines.append(f"      - {workdir}/{functions_dir}/node_modules")
        if inject_ide:
            home = home_dir(runtime, manifest)
            lines.append(f"      - ~/.config/nvim:{home}/.config/nvim")
            lines.append(f"      - ./.nvim/share/nvim:{home}/.local/share/nvim")
            lines.append(f"      - ./.nvim/state/nvim:{home}/.local/state/nvim")
        lines.append("      - ./.opencode/skills:/var/devbox/skills:ro")
        lines.append("    environment:")
        lines.append(f"      - DEVBOX_SERVICE={name}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def plan_file_actions(project_root, cfg, compose_path, opts, plan):
    actions = []
    config_path = os.path.join(project_root, "devbox.json")
    actions.append(
        (config_path, "create" if not os.path.exists(config_path) else "skip")
    )

    for path, runtime in pending_dockerfiles(plan, project_root):
        if os.path.exists(path):
            actions.append((path, "skip (exists)"))
        else:
            actions.append((path, f"create (from {runtime} template)"))

    base = os.path.join(project_root, "docker-compose.yml")
    if compose_path is not None:
        actions.append(
            (base, f"skip (using existing {os.path.basename(compose_path)})")
        )
    else:
        actions.append((base, "create" if not os.path.exists(base) else "skip"))

    dev = os.path.join(project_root, "docker-compose.dev.yml")
    if not os.path.exists(dev):
        actions.append((dev, "create"))
    elif opts["yes"]:
        actions.append((dev, "skip (exists)"))
    else:
        actions.append((dev, "update"))

    actions.append((architecture_target(project_root), "update-block"))
    actions.append((os.path.join(project_root, ".gitignore"), "update-block"))
    actions.append((os.path.join(project_root, "ai") + os.sep, "ensure-dir"))
    return actions


def render_summary(actions, plan, opts):
    lines = ["", "Summary:"]
    for path, status in actions:
        lines.append(f"  {status:<22} {path}")
    lines.append("")
    lines.append("Service mapping:")
    lines.append(render_services_table(plan))
    lines.append("")
    lines.append("Next steps:")
    lines.append("  1. source build/envsetup.sh")
    lines.append("  2. devbox-up")
    lines.append("  3. (optional) devbox-ai-setup")
    lines.append("")
    lines.append(
        "Note: container UID/GID come from DEVBOX_UID/DEVBOX_GID, which "
        "envsetup.sh exports."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _may_write_architecture(project_root, interactive):
    target = architecture_target(project_root)
    if not os.path.exists(target):
        return True
    try:
        with open(target, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return True
    if MD_BEGIN in content:
        return True
    if not interactive:
        return True
    return confirm(f"{target} exists without a DevBox block; append one?", default=True)


def _block_status(status, existed):
    """Map ``upsert_marked_block`` output to a user-facing status."""
    if status == "created":
        return "block added" if existed else "block created"
    if status == "updated":
        return "block updated"
    return status


def write_outputs(project_root, cfg, compose_path, opts, manifest, interactive,
                  extras, reused_binds, reuse, plan):
    actions = []
    build_dir = _build_dir()

    config_path = os.path.join(project_root, "devbox.json")
    if reuse:
        actions.append((config_path, "kept existing"))
    else:
        save_config(project_root, cfg)
        actions.append((config_path, "created"))

    for path, runtime in pending_dockerfiles(plan, project_root):
        content = render_root_dockerfile(manifest, runtime, build_dir)
        if content is None:
            actions.append((path, "skipped (no template)"))
        else:
            status = write_file_safely(path, content)
            actions.append((path, f"{status} (from {runtime} template)"))

    base = os.path.join(project_root, "docker-compose.yml")
    if compose_path is not None:
        actions.append((base, f"kept existing compose ({os.path.basename(compose_path)})"))
    else:
        actions.append((base, write_file_safely(base, render_compose_base(cfg))))

    dev = os.path.join(project_root, "docker-compose.dev.yml")
    content = render_compose_dev(cfg, manifest, extras, reused_binds)
    if not os.path.exists(dev):
        actions.append((dev, write_file_safely(dev, content)))
    elif opts["yes"]:
        actions.append((dev, "skipped (exists)"))
    else:
        with open(dev, encoding="utf-8") as f:
            current = f.read()
        if current == content:
            actions.append((dev, "unchanged"))
        else:
            diff = difflib.unified_diff(
                current.splitlines(),
                content.splitlines(),
                fromfile="docker-compose.dev.yml (current)",
                tofile="docker-compose.dev.yml (proposed)",
                lineterm="",
            )
            print("\n".join(diff))
            if confirm("Overwrite docker-compose.dev.yml? (a .bak is kept)", default=False):
                actions.append(
                    (dev, write_file_safely(dev, content, overwrite=True))
                )
            else:
                actions.append((dev, "skipped (kept existing)"))

    if extras.get("inject_ide"):
        print(
            "\nNote: --inject-ide mounts your Neovim config; the service image must "
            "contain nvim (and lazygit) at runtime."
        )

    if _may_write_architecture(project_root, interactive):
        arch_path = architecture_target(project_root)
        existed = os.path.exists(arch_path)
        status = upsert_marked_block(
            arch_path, render_architecture(cfg), MD_BEGIN, MD_END
        )
        actions.append((arch_path, _block_status(status, existed)))
    else:
        actions.append((architecture_target(project_root), "skipped"))

    gitignore = os.path.join(project_root, ".gitignore")
    if not interactive or confirm("Add DevBox entries to .gitignore?", default=True):
        existed = os.path.exists(gitignore)
        status = upsert_marked_block(
            gitignore, ".opencode/\n.nvim/\n", HASH_BEGIN, HASH_END
        )
        actions.append((gitignore, _block_status(status, existed)))
    else:
        actions.append((gitignore, "skipped"))

    ai_dir = os.path.join(project_root, "ai")
    os.makedirs(ai_dir, exist_ok=True)
    actions.append((ai_dir + os.sep, "ensured"))

    return actions


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(project_root, opts):
    interactive = not opts["yes"] and not opts["dry_run"]
    build_dir = _build_dir()
    manifest = load_manifest(build_dir)

    existing = None
    existing_version = None
    config_path = os.path.join(project_root, "devbox.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                existing_version = json.load(f).get("version")
        except (OSError, ValueError):
            existing_version = None
        try:
            existing = load_config(project_root)
        except ConfigError as exc:
            print(f"error: {exc}")
            return 1
        print(f"\nFound an existing devbox.json (version={existing_version}).")
        if existing_version != 2:
            print(
                "The configuration is normalized to the v2 schema in memory; the "
                "existing file is kept."
            )
        print("Adopt keeps this configuration and only (re)generates missing files.")

    reuse = existing is not None
    if reuse and interactive:
        reuse = not confirm("Re-run discovery and scan for services again?", default=False)

    compose_path = find_compose_file(project_root)
    generated_files = []
    reused_binds = set()

    if reuse:
        cfg = existing
        if opts["inject_ide"]:
            cfg["inject_ide"] = True
        plan = [
            {
                "service": service,
                "kind": "build" if service.get("source") else "image",
                "reused_bind": None,
                "warnings": [],
                "analysis": None,
            }
            for service in cfg.get("services", [])
        ]
        extras = {
            "inject_ide": bool(cfg.get("inject_ide")),
            "has_firebase": bool(cfg.get("has_firebase")),
            "functions_dir": cfg.get("functions_dir"),
            "preserve_env": cfg.get("preserve_env") or [],
        }
        generated_files = list(cfg.get("generated_files") or [])
    else:
        plan, generated_files = discover_plan(
            project_root, compose_path, manifest, opts, interactive
        )
        if plan is None:
            return 1
        extras = collect_extras(project_root, interactive, opts)
        reused_binds = {
            entry["service"]["name"]
            for entry in plan
            if entry.get("reused_bind") is not None
        }
        extra = {}
        if extras["preserve_env"]:
            extra["preserve_env"] = extras["preserve_env"]
        if extras["has_firebase"]:
            extra["has_firebase"] = True
        if extras["functions_dir"]:
            extra["functions_dir"] = extras["functions_dir"]
        if generated_files:
            extra["generated_files"] = generated_files
        cfg = new_config(
            "adopt",
            [entry["service"] for entry in plan],
            inject_ide=extras["inject_ide"],
            extra=extra,
        )

    print(render_plan(plan))

    actions = plan_file_actions(project_root, cfg, compose_path, opts, plan)
    if opts["dry_run"]:
        print("\nPlanned file actions (dry run):")
        for path, status in actions:
            print(f"  {status:<22} {path}")
        print("\nDry run: no files were written.")
        return 0

    actions = write_outputs(
        project_root, cfg, compose_path, opts, manifest, interactive,
        extras, reused_binds, reuse, plan,
    )
    print(render_summary(actions, plan, opts))
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print(USAGE)
        return 0
    try:
        opts = parse_args(args)
    except UsageError as exc:
        print(f"error: {exc}")
        print(USAGE)
        return 2

    project_root = opts["project_root"]
    if not os.path.isdir(project_root):
        print(f"error: {project_root} is not a directory")
        return 1
    return run(project_root, opts)


if __name__ == "__main__":
    sys.exit(main())
