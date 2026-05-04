---
name: devbox-core
description: Essential operating rules for AI agents executing tasks within the DevBox containerized platform via the MCP Gateway.
---

# 🏗️ DevBox Orchestration Core

You are operating within "DevBox," a highly secure, containerized Internal Developer Platform (IDP). You are not running directly on the user's host operating system. Instead, you are connected via a Model Context Protocol (MCP) gateway running inside a Docker container.

To successfully assist the developer, you MUST strictly adhere to the following architectural rules:

## 1. Execution Rules (The MCP Gateway)
* **NEVER** attempt to run bash commands directly on the local host system. You do not have host access.
* **ALWAYS** use the MCP tools provided to execute commands.
* When executing a command, you must explicitly declare the `target_container`. Use your MCP tools to query the live list of available containers for this specific project.

## 2. Dynamic Container Discovery & Mapping
This platform is tech-agnostic. The specific containers, languages, and frameworks depend entirely on the developer's project.
* **To understand the project stack:** Read the `ARCHITECTURE.md` file and `docker-compose.yml` in the project root to learn what services exist.
* **Source Code Mapping:** The host directory generally maps to the `/app` directory inside the respective containers (e.g., host `src/<service_name>/` usually maps to container `/app/`). Check the compose file volumes to verify.

## 3. Network Discovery
* The containers communicate on an isolated, internal Docker network. 
* To reach one container from another, use its Docker Compose service name as the hostname (e.g., `http://<service_name>:<port>`). Do not use `localhost` for container-to-container communication.

## 4. Port Exposing
* If you create or start a new development server (e.g., Vite, Django, Go), it must bind to `0.0.0.0`, NOT `127.0.0.1` or `localhost`. If it binds to localhost inside the container, the developer will not be able to reach it from their host machine's browser.

## 5. File System Edits
* When editing code, use your standard file-writing capabilities on the host paths (e.g., `src/...`). The Docker volume mounts will instantly sync these changes into the running containers. You do not need to execute bash commands to edit files.

## 6. Third-Party Skill Execution (The "Script Bridge" Rule)
When utilizing external skills (e.g., from Anthropic or other vendors), those skills may instruct you to run local scripts like `bash scripts/setup.sh`. 

Because you are executing commands inside isolated Docker containers via MCP, the container does not have access to the skill's source folder. Whenever a skill asks you to run a script, you MUST perform this "Script Bridge" maneuver:

1. **Locate the Script:** Find the script on the host file system. It will typically be inside `.opencode/skills/<skill_name>/scripts/`.
2. **Bridge to the Container:** Use your file system tools to copy that `scripts/` directory into the target service's source code directory (e.g., copy it to `src/frontend/scripts/`).
3. **Execute via MCP:** Now that the script is mapped into the container's volume, execute it using the MCP Gateway targeting the correct container (e.g., run `bash /app/scripts/setup.sh` inside the `frontend` container).
