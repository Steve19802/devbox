# 🏗️ DevBox: Infrastructure & AI Toolchain

Welcome to the **DevBox Platform**. This directory (`build/`) contains the immutable infrastructure, build scripts, and standard AI toolchains that power this project's local development environment.

## 🌟 Core Philosophy

This platform was engineered using enterprise-grade platform principles:

1. **Zero Host Dependencies:** You only need Docker installed on your machine. No Python virtual environments, no Node version managers, no local databases polluting your host OS.
2. **Strict Separation of Concerns:**
   - `build/` = Immutable Infrastructure, Template Engine, & Standard AI Tools.
   - `src/` = Business Logic & Application Code.
   - `ai/` = Mutable, Project-Specific AI Overrides.
3. **AI-Native & Secure:** AI agents are granted orchestration powers via a sandboxed Model Context Protocol (MCP) server that dynamically discovers project containers.
4. **Dev vs. Prod Parity:** We use Multi-Stage Dockerfiles to ensure that your production image is tiny and secure, while your local development container is packed with hot-reloading and IDE tools.

---

## 📂 Architecture Overview

```text
my-app/ (Project Root)
│
├── .env                      <-- Auto-generated: Host UID/GID & Tool versions
├── docker-compose.yml        <-- LAYER 1: Production Base (Pure App)
├── docker-compose.dev.yml    <-- LAYER 2: Dev Overrides (Volume Mounts)
├── ARCHITECTURE.md           <-- Auto-generated documentation for the app developers
│
├── build/                    <-- 🏗️ INFRASTRUCTURE (You are here)
│   ├── envsetup.sh           <-- The CLI router & autocompletion engine
│   ├── docker-compose.mcp.yml<-- LAYER 3: Platform Infra (DevBox MCP)
│   ├── devbox-mcp/           <-- The Python MCP Docker Gateway
│   ├── scripts/              <-- Python CLI scripts (scaffold.py, ai_setup.py, compile_ai.py)
│   ├── templates/            <── Template engine (manifest.json, dockerfiles/, compose/)
│   └── ai/                   <-- The "Standard Library" of AI tools
│
└── src/                      <-- 💻 BUSINESS LOGIC
    ├── <service_1>/          <-- Multi-stage Dockerfile
    ├── <service_2>/          <-- Multi-stage Dockerfile
    └── ...
```

---

## 🚀 Getting Started

If you are cloning this repository as a fresh template, run the following commands from the project root:

### 1. Load the Environment
```bash
source build/envsetup.sh
```
*This loads the `devbox-*` CLI suite into your active terminal and enables **Tab Autocompletion** for services and tools.*

### 2. Scaffold the Project
```bash
devbox-init
```
*This triggers the Python scaffolding engine. It interactively asks how many services your project needs, their names, runtimes, and ports. It securely maps your host UID/GID to prevent Linux permission errors, generates multi-stage Dockerfiles, dynamic docker-compose files, and optionally injects the Containerized IDE (Neovim/Lazygit) into your images.*

*You can also script it non-interactively:*
```bash
devbox-init --service api:node:3000 --service worker:python:8000 --inject-ide
```

### 3. Compile your AI Toolchain
```bash
devbox-ai-setup
```
*You will be prompted to select your preferred toolchain (e.g., OpenCode, Cursor). This triggers `build/scripts/compile_ai.py` to merge base and project-specific AI settings.*

### 4. Boot the Environment
```bash
devbox-up
```

---

## 💻 The Containerized IDE Injection

By default, the `devbox-init` script generates lightweight, multi-stage Dockerfiles. During initialization, it will ask if you want to inject the Containerized Terminal IDE.

If you choose **Yes** (or pass `--inject-ide`):
* The scaffolding engine safely injects `Neovim`, `Lazygit`, and `LSP dependencies` into the `dev` stage of each service's Dockerfile via the `@DEVBOX_IDE_INJECTION_POINT@` marker.
* It strictly maintains the non-root user context for each runtime so you never have file permission conflicts on your host machine.
* To launch the IDE inside a running container, simply type: `devbox-edit <service_name>`.

If you choose **No**:
* The containers remain hyper-lightweight, perfectly optimized for connecting via VS Code DevContainers or JetBrains Gateway.
* Neovim volumes and `.nvim/` directories are not created.

---

## 🛠️ CLI Command Reference

*(Tip: Press `TAB` after typing these commands to auto-complete service names!)*

* **`devbox-init [--service name:runtime:port] [--inject-ide]`**: Scaffolds the project with interactive wizard or CLI flags.
* **`devbox-ai-setup [tool_name]`**: Compiles the AI configuration (interactive or direct).
* **`devbox-build [args]`**: Builds the Docker containers across all 3 layers. 
* **`devbox-up [args]`**: Starts the environment (attached by default; pass `-d` for detached mode).
* **`devbox-down [args]`**: Gracefully stops the environment.
* **`devbox-logs [service]`**: Tails logs for a specific service (or all services).
* **`devbox-ps [service]`**: Table of the long-running processes the AI started (`SERVICE ID NAME STATUS UPTIME COMMAND`).
* **`devbox-ps logs <service> <id> [-f]`**: Shows the last 200 lines of an AI-started process, or follows it until it exits with `-f`.
* **`devbox-ps stop <service> <id> [grace]`**: Stops an AI-started process (default grace: 10s).
* **`devbox-run <service> <command>`**: Executes a command inside a specific running container.
* **`devbox-edit <service>`**: Attaches the containerized IDE to the target service.

---

## 🤖 The AI Overlay System

To support multiple AI IDEs, we use an **Overlay Pattern**. When you run `devbox-ai-setup`, an ephemeral Python container runs `build/scripts/ai_setup.py` (which imports `compile_ai.py`) to:
1. Show an interactive menu of available AI tools (or accept a CLI argument).
2. Copy the "Base" tools from `build/ai/`
3. Copy the "Project" tools from `ai/`, overwriting base tools.
4. Perform a deep JSON merge on configuration files.

---

## 🛡️ The DevBox MCP (Docker Gateway)

The AI connects to an isolated Python container running an HTTP Server-Sent Events (SSE) server on Docker's internal network.

**Capabilities & Security:**
* **Self-Discovery:** The MCP reads the Docker socket and dynamically feeds the AI a live list of sibling containers it is allowed to interact with.
* **Collision-Proof:** The container dynamically resolves its own network name, meaning you can run multiple DevBox projects on the same host machine simultaneously without conflicts.
* **The Warden:** The AI cannot interact with containers outside of this project, nor can it execute commands inside the MCP container itself or on your host OS.

### 🧰 MCP Tools

| Tool | Purpose |
| --- | --- |
| `execute_container_command` | Run a short command in a container (configurable `timeout`). |
| `start_process` | Start a long-running command (dev server/watcher) detached; optionally wait for a `ready_pattern`. |
| `list_processes` | List supervised processes, in one service or across all running services. |
| `get_process_output` | Read a started process's log; page forward with `offset=next_offset`. |
| `stop_process` | Stop a started process (SIGTERM the group, SIGKILL after grace). |
| `list_services` | List in-scope services: status, health, uptime, restart count. |
| `service_logs` | Read a container's stdout+stderr from container start (`tail`/`since`). |
| `restart_service` / `stop_service` / `start_service` | Restart/stop/start a service's container by ID. |

Builds, container re-creation, `docker compose down`, and volume changes are intentionally **not** exposed; run `devbox-up`, `devbox-rebuild`, or `devbox-build` on the host instead.

### 🔒 Scope Policy

A container is in scope only if all of the following hold, checked inside the MCP server at every call (never cached):
* It carries the **same `com.docker.compose.project` label** as the MCP container.
* It is attached to a network the MCP container is also on **and** that network belongs to the same compose project.
* It is **not the MCP container itself**.
* Its service is **not protected** (`mcp.protected_services`).

Anything else resolves to `Service '<name>' not found or not allowed.` This policy is enforced inside the MCP container, which is the **only** component with the Docker socket mounted — the AI container must never receive it.

### 🔄 In-Container Process Model

Long-running processes are supervised from inside each target container by `build/devbox-mcp/devbox-proc.sh`, which is streamed over stdin and never installed on disk:
* State lives in `${DEVBOX_PROC_DIR:-/tmp/devbox-proc}/<id>/` inside the container, so processes **survive MCP restarts and agent sessions**.
* That state is part of the container filesystem, so it is **lost when the container is re-created** (e.g. a `devbox-up`/`devbox-rebuild` after a compose change).
* Detached processes require **`setsid`**: present in Debian images via `util-linux`, and in Alpine via `busybox`. Without it, `start_process` fails with an install hint.
* Add **`init: true`** to your services so finished children get reaped instead of accumulating as zombies.

### ⚙️ Configuration

`devbox-init` and `devbox-adopt` generate an optional `mcp` section in `devbox.json` (all keys optional; shown with their defaults):

```json
"mcp": {
  "protected_services": [],
  "exec":      { "default_timeout": 180, "max_timeout": 3600, "max_output_kb": 64 },
  "processes": { "max_per_container": 5, "max_log_mb": 10 }
}
```

| Key | Meaning | Default | Hard cap |
| --- | --- | --- | --- |
| `mcp.protected_services` | Services the AI may never touch | `[]` | — |
| `mcp.exec.default_timeout` | Default `execute_container_command` timeout (s) | `180` | `max_timeout` |
| `mcp.exec.max_timeout` | Maximum allowed timeout (s) | `3600` | `3600` |
| `mcp.exec.max_output_kb` | Output kept per stream (KB) | `64` | `1024` |
| `mcp.processes.max_per_container` | Max supervised processes per container | `5` | `20` |
| `mcp.processes.max_log_mb` | Per-process log cap before rotation (MB) | `10` | `100` |

Out-of-range or invalid values fall back to the default (or clamp to the cap) and log a warning.

**How it is applied:** `envsetup.sh` reads `devbox.json` and exports `DEVBOX_MCP_*` environment variables, and `docker-compose.mcp.yml` passes them into the MCP container. Settings are read **once at MCP startup** — run `devbox-up` to apply changes (Compose re-creates the MCP container when its environment changes).

