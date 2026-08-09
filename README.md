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

