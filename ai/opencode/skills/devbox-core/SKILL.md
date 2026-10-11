---
name: devbox-core
description: Essential operating rules for AI agents executing tasks within the DevBox containerized platform via the MCP Gateway.
---

# 🏗️ DevBox Orchestration Core

You are operating within "DevBox," a highly secure, containerized Internal Developer Platform (IDP). You are not running directly on the user's host operating system. Instead, you are running in an isolated container connected via a Model Context Protocol (MCP) gateway to the rest of the project containers.

To successfully assist the developer, you MUST strictly adhere to the following architectural rules:

## 📂 Project Layout & Path Mapping (CRITICAL)

DevBox does **not** impose a directory layout. The `src/<service>/ -> /app` layout is only the default for projects created by `devbox-init`. An adopted project keeps whatever layout it already has — for example `apps/web -> /usr/src/app`, `services/api -> /srv/api`, or the whole project root `. -> /app`. **Never move, rename or restructure the user's folders to "fit" DevBox**: the container is configured to match the project, not the other way around. Services marked `"managed": false` in `devbox.json` are **user-owned**; leave their source folders, Dockerfiles and `docker-compose.yml` entries alone.

### How to find the real mapping (authoritative)

Call the MCP `list_services` tool. Each entry reports the container `workdir` and its project `mounts` as a list of `{host, container}` pairs, where `host` is a path relative to the project root (the folder you edit) and `container` is the absolute path inside that service's container:

```json
{
  "service": "web",
  "container": "my-app-web-1",
  "running": true,
  "workdir": "/usr/src/app",
  "mounts": [ { "host": "apps/web", "container": "/usr/src/app" } ]
}
```

This runtime output is the **source of truth**. Secondary sources (may be stale, so use them only to double-check):
* `devbox.json` — each `services[]` entry has `source` (host folder) and `workdir` (container path).
* `ARCHITECTURE.md` — DevBox generates a services table with the same host -> container mapping (stored in `DEVBOX.md` when you keep your own `ARCHITECTURE.md`).
* `docker-compose.yml` / `docker-compose.dev.yml` — the raw `volumes:` entries.

The platform is tech-agnostic, so read the actual project instead of assuming a stack. Services may be defined in either compose file (e.g. a `playwright` service that exists only in `docker-compose.dev.yml`), and the languages/frameworks depend entirely on the developer's project.

### Path translation rule

You edit files in the host folder; MCP commands run in the container. Translate through the matching mount:

* container path = `<mount.container>/<rest>`
* host path = `<project_root>/<mount.host>/<rest>`

Worked example, using the `list_services` output above (`apps/web` mounted at `/usr/src/app`):

```bash
# You run this via MCP in the web container:
execute_container_command(container="web", command="mkdir /usr/src/app/src/test")
# That file now exists on the host at:
#   <project_root>/apps/web/src/test
```

**Boundaries:**
* A path is only shared if it is inside one of that service's `mounts`. Files outside every mount are **not** visible to the container, and files the container writes outside its mounts are **not** visible to you.
* A service with an empty `mounts` list shares **no** files with you: anything it writes is trapped in the container and lost when it is re-created.
* `workdir` is where a command starts inside the container and is usually (but not always) inside a mount. If it is not inside any mount, treat the whole container filesystem as ephemeral except for the listed mounts.

### File edits sync automatically

When editing code, use your standard file-writing capabilities on the host path (e.g. `<project_root>/apps/web/src/...`). The Docker volume mounts sync those changes into the running containers instantly — you do not need to run bash commands to edit files.

## The 3-Layer Docker System
We use a 3-tier architecture to maintain production parity:
1. `docker-compose.yml` defines the core production services.
2. `docker-compose.dev.yml` injects development tools and volume mounts.
3. `build/docker-compose.mcp.yml` silently injects AI capabilities in the background.

Please edit `docker-compose.dev.yml` for local testing, and only modify `docker-compose.yml` when adding new production services.
**Important**: If you are running, it means that the docker-compose.dev.yml is already up and running

## Execution Rules (The MCP Gateway)
* **NEVER** attempt to run bash commands directly on the local host system. You do not have host access.
* **ALWAYS** use the MCP tools provided by the `devbox-mcp` server to execute commands on target container.
* **Targeting Containers:** The MCP `execute_container_command` tool uses "Smart Routing." You do not need to look up complex Docker IDs or query a list. Simply pass the generic service name (e.g., `api` or `worker`) as the `container` parameter, and the MCP Gateway will automatically route it to the correct running instance.
* **Long-Running Processes**: For dev servers, watchers, or anything that does not exit on its own, use `start_process` — never background with `&`/`nohup` inside `execute_container_command`. For short commands, `execute_container_command` has a configurable `timeout`. See the **Long-Running Processes & Service Control** section below.

## Long-Running Processes & Service Control

**Choosing the right tool:**
* **Short commands** (installs, tests, a build step, git, file ops) → `execute_container_command`. Its `timeout` parameter defaults to 180s and is capped at `max_timeout`; both are configurable in `devbox.json` (check the tool schema for the live values). Raise it for longer tasks (e.g. `timeout=600`).
* **Dev servers, watchers, anything that does not exit** → `start_process`. **NEVER** background with `&`/`nohup` inside `execute_container_command`: the call blocks until its timeout and then the whole process group is killed, or the process keeps running untracked with its output lost.

**Recommended workflow:**
1. `list_processes` first, to see what is already running and avoid starting duplicates. Like `docker ps`, it only shows running processes; pass `all=true` to also see exited/lost ones (e.g. to check why something died).
2. `start_process` with a meaningful `name` and a `ready_pattern` so you know it actually came up — e.g. Vite `Local:.*http`, Uvicorn `Uvicorn running on`.
3. `get_process_output` with `offset=next_offset` for incremental log reads instead of re-reading the whole log.
4. `stop_process` when you are done, or before restarting a process.

Processes persist after your session ends. When you finish, tell the user what you left running (they can inspect it from their host terminal with `devbox-ps`).

**Binding:** any server you start must bind `0.0.0.0`, not `127.0.0.1`/`localhost`, or the developer cannot reach it from their host machine.

**Service control:**
* `list_services` — the in-scope services with status, health, uptime, restart count, working dir and mounted workspace folders.
* `service_logs` — a container's stdout+stderr **from container start**, ideal for diagnosing startup errors (works on stopped containers too).
* `restart_service` / `stop_service` / `start_service` — restart/stop/start a service's container. Restarting or stopping **kills any process you started with `start_process` in that container**.
* Builds, container re-creation, `docker compose down`, and volume changes are **not** available via MCP. Ask the user to run `devbox-up`, `devbox-rebuild`, or `devbox-build` on the host.

**Scope:** only services of the current DevBox project are reachable. Anything else answers `Service '<name>' not found or not allowed.` — do not try to work around it. Some services may additionally be protected by the user and answer the same way.

**Limits:** the number of processes per container and each process's log size are capped by the user's configuration; `start_process` reports the limit when it is reached. If you hit the process cap, `stop_process` something first to free a slot.

**Guardrails:** **NEVER** modify the `mcp` section of `devbox.json` — it defines your own scope and limits. If a limit blocks the task, ask the user.

## Network Discovery
* The containers communicate on an isolated, internal Docker network. 
* To reach one container from another, use its Docker Compose service name as the hostname (e.g., `http://<service_name>:<port>`). Do not use `localhost` for container-to-container communication.

## Port Exposing
* If you create or start a new development server (e.g., Vite, Django, Go), it must bind to `0.0.0.0`, NOT `127.0.0.1` or `localhost`. If it binds to localhost inside the container, the developer will not be able to reach it from their host machine's browser.

## Third-Party Skill Execution (The Skill Mount)
When utilizing external skills (e.g., from Anthropic), those skills may instruct you to run local scripts like `bash scripts/setup.sh`. 

Because you are executing commands via MCP, you must run those scripts inside the target container. All compiled skills and their scripts are automatically volume-mounted into every container at `/var/devbox/skills/`.

When a skill asks you to run a script, simply execute it directly from the mount. 
For example, if the `web-artifacts-builder` skill wants to run `init-artifact.sh`, you MUST use the MCP Gateway to execute this command inside the appropriate container:
`bash /var/devbox/skills/web-artifacts-builder/scripts/init-artifact.sh <project-name>`

## System Dependencies & Privilege Escalation
* **The Non-Root Rule:** You execute commands as a non-root user to protect the developer's host file system. 
* **The Sudo Escape Hatch:** If you absolutely must install a global system package (e.g., `apt-get install`, global `npm` installs) and encounter `EACCES` permission errors, you have passwordless `sudo` access. Prefix your execution command with `sudo`.
* **CRITICAL - Developer Notification:** Containers are ephemeral. Any package you install via `sudo` will be lost when the container is rebuilt. If you use `sudo` to install a package or modify system configurations, you MUST explicitly notify the developer in your chat response. Tell them exactly what you installed and suggest they add it to the project's `Dockerfile` to make the change permanent.
