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
│   ├── scripts/              <-- Standalone Python compilation scripts
│   ├── templates/            <-- Base Dockerfiles, Compose files, and injections
│   └── ai/                   <-- The "Standard Library" of AI tools
│
└── src/                      <-- 💻 BUSINESS LOGIC
    ├── frontend/             <-- Multi-stage Dockerfile
    └── backend/              <-- Multi-stage Dockerfile
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
*This triggers the Template Engine. It securely maps your host UID/GID to prevent Linux permission errors, generates the base Multi-Stage Dockerfiles, and will interactively ask if you want to inject the Containerized IDE (Neovim/Lazygit) into your images.*

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

By default, the `devbox-init` script generates lightweight, multi-stage Dockerfiles. However, during initialization, it will ask if you want to inject the Containerized Terminal IDE.

If you choose **Yes**:
* The template engine safely injects `Neovim`, `Lazygit`, and `LSP dependencies` into the `dev` stage of your Dockerfiles via the `@DEVBOX_IDE_INJECTION_POINT@` marker.
* It strictly maintains the non-root user context (`node` or `devuser`) so you never have file permission conflicts on your host machine.
* To launch the IDE inside a running container, simply type: `devbox-edit <service_name>` (e.g., `devbox-edit backend`).

If you choose **No**:
* The containers remain hyper-lightweight, perfectly optimized for connecting via VS Code DevContainers or JetBrains Gateway.

---

## 🛠️ CLI Command Reference

*(Tip: Press `TAB` after typing these commands to auto-complete service names!)*

* **`devbox-init`**: Scaffolds the base project structure using the template engine.
* **`devbox-ai-setup`**: Compiles the AI configuration.
* **`devbox-build [args]`**: Builds the Docker containers across all 3 layers. 
* **`devbox-up [args]`**: Starts the environment.
* **`devbox-down [args]`**: Gracefully stops the environment.
* **`devbox-logs [service]`**: Tails logs for a specific service (or all services).
* **`devbox-run <service> <command>`**: Executes a command inside a specific running container.
* **`devbox-edit <service>`**: Attaches the containerized IDE to the target service.

---

## 🤖 The AI Overlay System

To support multiple AI IDEs, we use an **Overlay Pattern**. When you run `devbox-ai-setup`, an ephemeral Python container runs `build/scripts/compile_ai.py` to:
1. Copy the "Base" tools from `build/ai/`
2. Copy the "Project" tools from `ai/`, overwriting base tools.
3. Perform a deep JSON merge on configuration files.

---

## 🛡️ The DevBox MCP (Docker Gateway)

The AI connects to an isolated Python container running an HTTP Server-Sent Events (SSE) server on Docker's internal network.

**Capabilities & Security:**
* **Self-Discovery:** The MCP reads the Docker socket and dynamically feeds the AI a live list of sibling containers it is allowed to interact with.
* **Collision-Proof:** The container dynamically resolves its own network name, meaning you can run multiple DevBox projects on the same host machine simultaneously without conflicts.
* **The Warden:** The AI cannot interact with containers outside of this project, nor can it execute commands inside the MCP container itself or on your host OS.

