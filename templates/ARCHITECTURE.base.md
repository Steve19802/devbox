# Project Architecture

Welcome to the project! This repository uses the **DevBox Platform** for local development, which enforces a strict separation between business logic and development infrastructure.

## Directory Structure

    .
    ├── ai/                       <-- Project-specific AI overrides (agents/MCPs)
    ├── build/                    <-- Immutable DevBox Infrastructure (Git Submodule/Template)
    ├── src/                      <-- Application Code
    │   ├── <service_1>/          <-- Service 1 (e.g., api, frontend, worker)
    │   ├── <service_2>/          <-- Service 2
    │   └── ...
    │
    ├── docker-compose.yml        <-- Production Base (Pure app services, no dev tools)
    └── docker-compose.dev.yml    <-- Local Dev Overrides (Volume mounts, hot-reloading)

## How to Develop Locally

This project runs inside isolated Docker containers. **Do not install runtimes or databases on your host machine.**

1. **Load the CLI Tools:** `source build/envsetup.sh`
2. **Setup AI Tooling (Optional):** `devbox-ai-setup`
3. **Start the Environment:** `devbox-up`

### Working with the Code

All code inside `src/` is volume-mounted into the containers. Any changes you make will instantly hot-reload.

If you need to run a command inside a container (like installing a package or running tests), use the wrapper script:
* `devbox-run <service_name> <command>`

### The 3-Layer Docker System

We use a 3-tier architecture to maintain production parity:
1. `docker-compose.yml` defines the core production services.
2. `docker-compose.dev.yml` injects development tools and volume mounts.
3. `build/docker-compose.mcp.yml` silently injects AI capabilities in the background.

Please edit `docker-compose.dev.yml` for local testing, and only modify `docker-compose.yml` when adding new production services.
