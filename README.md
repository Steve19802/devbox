# 🏗️ DevBox: Infrastructure & AI Toolchain

Welcome to the **DevBox Platform**. This directory (`build/`) contains the immutable infrastructure, build scripts, and standard AI toolchains that power this project's local development environment.

## 🌟 Core Philosophy

This platform was engineered using enterprise-grade platform principles, heavily inspired by the Android Open Source Project (AOSP):

1. **Zero Host Dependencies:** You only need Docker installed on your machine. No Python virtual environments, no Node version managers, no local databases polluting your host OS.
2. **Strict Separation of Concerns:**
   - `build/` = Immutable Infrastructure & Standard AI Tools.
   - `src/` = Business Logic & Application Code.
   - `ai/` = Mutable, Project-Specific AI Overrides.
3. **AI-Native, Secure by Default:** AI agents (like OpenCode or Cursor) are granted orchestration powers via a sandboxed Model Context Protocol (MCP) server that dynamically discovers project containers without exposing your host machine.

---

## 📂 Architecture Overview

```text
my-app/ (Project Root)
│
├── .env                      <-- Auto-generated: Host UID/GID & Tool versions
├── docker-compose.yml        <-- LAYER 1: Production Base (Pure App)
├── docker-compose.dev.yml    <-- LAYER 2: Dev Overrides (Volume Mounts, Neovim)
├── .gitignore                <-- Ignores compiled AI configs (e.g., .opencode/)
│
├── build/                    <-- 🏗️ INFRASTRUCTURE (You are here)
│   ├── envsetup.sh           <-- The CLI compiler & router
│   ├── docker-compose.mcp.yml<-- LAYER 3: Platform Infra (DevBox MCP)
│   ├── devbox-mcp/           <-- The Python MCP Docker Gateway
│   └── ai/                   <-- The "Standard Library" of AI tools
│
├── ai/                       <-- 🧠 PROJECT AI (Mutable overrides)
│   └── opencode/             <-- Project-specific AI settings
│
└── src/                      <-- 💻 BUSINESS LOGIC
    ├── frontend/             
    └── backend/              
```

---

## 🚀 Getting Started

If you are cloning this repository as a fresh template, run the following commands from the project root:

### 1. Load the Environment
```bash
source build/envsetup.sh
```
*This loads the `devbox-*` CLI suite into your active terminal.*

### 2. Scaffold the Project
```bash
devbox-init
```
*This safely generates the `src/` folders, the `.env` file (mapping your host UID/GID to prevent permission errors), and the base Docker Compose files.*

### 3. Compile your AI Toolchain
```bash
devbox-toolchain
```
*You will be prompted to select your preferred toolchain (e.g., OpenCode, Cursor, or None). This compiles the AI infrastructure.*

### 4. Boot the Environment
```bash
devbox-up
```
*This spins up your frontend, backend, and the background MCP supervisor by merging the 3-layer architecture.*

---

## 🐳 The 3-Layer Docker Architecture

To achieve perfect "Dev vs. Prod Parity" while maintaining a great developer experience, this project uses Docker Compose's native multi-file overrides. When you run `devbox-up`, the CLI stacks three files together in memory:

1. **`docker-compose.yml` (Production Base):** Lives in the project root. Defines pure services, ports, and build targets. It contains zero local volume mounts or developer tools.
2. **`docker-compose.dev.yml` (Dev Overrides):** Lives in the project root. Modifies the base file to target the `dev` build stage, injects local `./src` volume mounts for hot-reloading, and attaches Neovim state mappings.
3. **`build/docker-compose.mcp.yml` (Platform Infra):** Hidden in the build repo. Atomically injects the AI MCP supervisor into the Docker network without polluting the project root.

---

## 🛠️ CLI Command Reference

* **`devbox-init`**: Scaffolds the base project structure, Docker files, and `.env` securely.
* **`devbox-toolchain [tool]`**: Compiles the base AI configuration with project-specific overrides.
* **`devbox-build [args]`**: Builds the Docker containers across all 3 layers. Accepts raw Compose flags.
* **`devbox-up [args]`**: Starts the environment. Accepts flags (e.g., `--remove-orphans`).
* **`devbox-down [args]`**: Gracefully stops the environment and handles AI network attachments safely.
* **`devbox-run <service> <command>`**: Executes a command inside a specific running container.
    * *Example: `devbox-run backend pytest`*
* **`devbox-edit <service>`**: Attaches a containerized IDE (Neovim) to the target service.

---

## 🤖 The AI Overlay System

To support multiple AI IDEs and prevent project-specific configurations from breaking the core infrastructure, we use an **Overlay Pattern** during compilation.

When you run `devbox-toolchain opencode`, a lightweight, ephemeral Python container performs the following:
1. Copies the "Base" tools from `build/ai/opencode/` (Agents, Skills).
2. Copies the "Project" tools from `ai/opencode/`, overwriting base tools if there is a naming conflict.
3. Performs a deep JSON merge on `opencode.json` (combining the Base MCP Gateway with Project-Specific MCPs).
4. Outputs the final, compiled configuration to a hidden `.opencode/` directory at the project root.

**Adding Project-Specific Tools:** Do not edit `build/ai/`. Instead, place your custom agents or JSON configurations in `<project-root>/ai/<tool>/`. They will be merged automatically next time you run the toolchain command.

---

## 🛡️ The DevBox MCP (Docker Gateway)

The crown jewel of this architecture is the **DevBox Supervisor** (`build/devbox-mcp`). 

Instead of allowing the AI to run blind bash commands on your host machine (which is a massive security risk), the AI connects to an isolated Python container running an HTTP Server-Sent Events (SSE) server on Docker's internal network.

**Capabilities & Security:**
* **Self-Discovery:** The MCP reads the Docker socket, finds its own Compose stack (`com.docker.compose.project`), and dynamically feeds the AI a live list of sibling containers it is allowed to interact with.
* **The Warden:** The AI cannot interact with containers outside of this project, nor can it execute commands inside the MCP container itself.
* **Language Agnostic:** The MCP provides a single `execute_container_command` tool. The AI reads your project's Dockerfiles to determine how to run commands (e.g., it knows to use `. .venv/bin/activate && pip` for Python, or `npm` for Node).
```
