---
name: devbox-core
description: Essential operating rules for AI agents executing tasks within the DevBox containerized platform via the MCP Gateway.
---

# 🏗️ DevBox Orchestration Core

You are operating within "DevBox," a highly secure, containerized Internal Developer Platform (IDP). You are not running directly on the user's host operating system. Instead, you are running in an isolated container connected via a Model Context Protocol (MCP) gateway to the rest of the project containers.

To successfully assist the developer, you MUST strictly adhere to the following architectural rules:

## 📂 Directory Structure

    .
    ├── ai/                       <-- 🧠 Project-specific AI overrides (agents/MCPs)
    ├── build/                    <-- ⚙️ Immutable DevBox Infrastructure (READ-ONLY)
    ├── src/                      <-- 💻 Application Code
    │   ├── [CONTAINER_NAME_1]    <-- App service 1
    │   ├── [CONTAINER_NAME_2]    <-- App service 2
    │   ├── ...
    │   └── [CONTAINER_NAME_n]    <-- App service n
    ├── docker-compose.yml        <-- 🚀 Production Base (Pure app services, no dev tools)
    └── docker-compose.dev.yml    <-- 🛠️ Local Dev Overrides (Volume mounts, hot-reloading)

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
* **Long-Running Processes**: The `execute_container_command` tool has a configurable `timeout` parameter (default: 180s). For quick commands, the default is fine. For longer operations, pass an explicit timeout (e.g., `timeout=600`). Do not start dev servers (e.g., `npm run dev`) via MCP even with a high timeout — instruct the user to run them in their host terminal via `devbox-run api "npm run dev"`.

## The 3-Way Path Mapping (CRITICAL)
You are running in an isolated AI container. When you use the MCP Gateway, you are executing commands in a *different* container. You must translate paths between these two worlds:

* **Your View (File Editing):** You access files relative to your current working directory (the project root). Example: `src/api/app.ts`.
* **The Target Container View (MCP Execution):** The target containers mount their specific source folder directly to `/app/`.
* **The Translation Rule:** When you run a command via MCP in any container, it executes inside `/app/`. If that command creates a file at `/app/new-file.txt`, you must use your file tools to edit it at `src/[CONTAINER_NAME]/new-file.txt`.

## Container Path Mapping (CRITICAL - Read Carefully)

You (the AI Agent) run in an isolated container with the project root mapped in.
There are THREE path perspectives you must understand:

### 1. Your Container (AI Agent) - Where you read/write files
- **Project root path (`PROJECT_ROOT`)**: This is the path where you were started. It follows this rule: `BOX/[HOST_NAME]/[PROJECT_NAME]`. Example: `/BOX/P14SNTBK/my-new-project/`
- **Containers path**: This is the path to the code and files of the containers. It follows this rule: `[PROJECT_ROOT]/src/[CONTAINER_NAME]`.

### 2. Target Containers - Where MCP commands execute
- **Container working path**: Unless otherwise specified, the base path for containers is `/app/`. Each container maps `[PROJECT_ROOT]/src/[CONTAINER_NAME]/` to `/app/`.

### 3. Path Translation Rule
When you execute a command via MCP that creates files at `/app/foo` in the `[CONTAINER_NAME]` container,
those files will appear at `[PROJECT_ROOT]/src/[CONTAINER_NAME]/foo` from your perspective.

**Example:**
```bash
# You run this via MCP in the api container:
devbox-mcp_execute_container_command(container="api", command="mkdir /app/test")
# You then access it at:
read(filePath="/BOX/P14SNTBK/my-new-project/src/api/test")
```

### 4. Verification Step (Do this once per session)
Read `docker-compose.dev.yml` to confirm exact volume mappings:
```bash
read(filePath="/BOX/P14SNTBK/my-new-project/docker-compose.dev.yml")
```

## Quick path reference
```
AI Container:    /BOX/[HOST_NAME]/[PROJECT_NAME]/src/[service]/
Target Container: /app/
```

## Dynamic Container Discovery & Mapping
This platform is tech-agnostic. The specific containers, languages, and frameworks depend entirely on the developer's project.
* **To understand the project stack:** Read the `ARCHITECTURE.md` file, `docker-compose.yml`, **and** `docker-compose.dev.yml` in the project root to learn what services exist. Services may be defined in either file (e.g., a `playwright` service in `docker-compose.dev.yml`).
* **Source Code Mapping:** The host directory generally maps to the `/app` directory inside the respective containers (e.g., host `src/<service_name>/` usually maps to container `/app/`). Check the compose file volumes to verify.

## Network Discovery
* The containers communicate on an isolated, internal Docker network. 
* To reach one container from another, use its Docker Compose service name as the hostname (e.g., `http://<service_name>:<port>`). Do not use `localhost` for container-to-container communication.

## Port Exposing
* If you create or start a new development server (e.g., Vite, Django, Go), it must bind to `0.0.0.0`, NOT `127.0.0.1` or `localhost`. If it binds to localhost inside the container, the developer will not be able to reach it from their host machine's browser.

## File System Edits
* When editing code, use your standard file-writing capabilities on the host paths (e.g., `src/...`). The Docker volume mounts will instantly sync these changes into the running containers. You do not need to execute bash commands to edit files.

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
