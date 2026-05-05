import asyncio
import subprocess
import socket
import uvicorn
from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp.server.sse import SseServerTransport


def get_container_mapping() -> dict[str, str]:
    """
    Returns a mapping of { 'compose_service_name': 'actual_docker_container_name' }
    e.g., {'frontend': 'my-new-project-frontend-1', 'backend': 'my-new-project-backend-1'}
    """
    try:
        # 1. Get our own ID
        container_id = socket.gethostname()

        # 2. Find our Docker Compose project name
        inspect_cmd = [
            "docker",
            "inspect",
            "-f",
            '{{index .Config.Labels "com.docker.compose.project"}}',
            container_id,
        ]
        project_result = subprocess.run(
            inspect_cmd, capture_output=True, text=True, check=True
        )
        project_name = project_result.stdout.strip()

        if not project_name:
            return {"error": "no_compose_project_found"}

        # 3. Filter docker ps for this project, outputting BOTH service name and container name separated by a pipe
        ps_cmd = [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
            "--format",
            '{{.Label "com.docker.compose.service"}}|{{.Names}}',
        ]
        ps_result = subprocess.run(ps_cmd, capture_output=True, text=True, check=True)

        # Build the dictionary mapping
        mapping = {}
        for line in ps_result.stdout.splitlines():
            if "|" in line:
                svc, name = line.split("|", 1)
                svc, name = svc.strip(), name.strip()
                if svc and name:
                    mapping[svc] = name

        # 4. Find the actual name of THIS MCP container so we can remove it
        my_name_cmd = ["docker", "inspect", "-f", "{{.Name}}", container_id]
        my_name_result = subprocess.run(my_name_cmd, capture_output=True, text=True)
        my_name = my_name_result.stdout.strip().lstrip("/")

        # Filter out ourselves
        return {svc: name for svc, name in mapping.items() if name != my_name}

    except Exception as e:
        return {"error": f"error_fetching_containers: {str(e)}"}


# Initialize the manual Server
mcp_server = Server("devbox-supervisor")


@mcp_server.list_tools()
async def handle_list_tools() -> list[Tool]:

    mapping = get_container_mapping()
    # The AI will only see the clean service names (e.g., "frontend", "backend")
    service_names = (
        list(mapping.keys()) if "error" not in mapping else ["error_fetching_services"]
    )

    return [
        Tool(
            name="execute_container_command",
            description=(
                "Execute a bash/shell command inside a specific Docker container."
                "CRITICAL PATH MAPPING: Commands run inside the target container's /app/ directory. "
                "Any files you create at /app/ will appear in your local workspace under src/<service_name>/."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "enum": service_names,
                        "description": "Target container name",
                    },
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                },
                "required": ["container", "command"],
            },
        ),
    ]


@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "execute_container_command":
            target_service = arguments["container"]
            command = arguments["command"]
            # Validate against the live list of containers
            mapping = get_container_mapping()

            # Security & Routing Check
            if target_service not in mapping:
                return [
                    TextContent(
                        type="text",
                        text=f"Security Violation: Service '{target_service}' not found or not allowed.",
                    )
                ]

            # SMART ROUTING: Translate "frontend" to "my-new-project-frontend-1"
            actual_container = mapping[target_service]

            # --- CENTRALIZED AUDIT LOGGING ---
            # Print to the MCP container's stdout so it appears in `docker logs`
            print(f"\n🤖 --- AI EXECUTING IN [{target_service}] ---", flush=True)
            print(f"Command: {command}", flush=True)

            try:
                # 1. Use async subprocess so the server doesn't freeze
                process = await asyncio.create_subprocess_exec(
                    *["docker", "exec", actual_container, "sh", "-c", command],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # 2. Enforce a strict 180 secs server-side timeout
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=3 * 60.0
                )

                stdout = stdout_bytes.decode()
                stderr = stderr_bytes.decode()
                returncode = process.returncode

                # Log the results
                print(f"Exit Code: {returncode}", flush=True)
                if stdout:
                    print(f"STDOUT:\n{stdout.strip()}", flush=True)
                if stderr:
                    print(f"STDERR:\n{stderr.strip()}", flush=True)
                print("-------------------------------------------\n", flush=True)

                return [
                    TextContent(
                        type="text",
                        text=f"Exit Code: {returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}",
                    )
                ]

            except asyncio.TimeoutError:
                # 3. If it takes too long, cleanly kill the process before the AI panics
                process.terminate()
                error_msg = "Execution Timed Out (180s limit). Process was cleanly terminated by the MCP Gateway. DO NOT run long-hanging processes or dev servers via MCP."

                print(f"❌ {error_msg}", flush=True)
                print("-------------------------------------------\n", flush=True)

                return [TextContent(type="text", text=error_msg)]

        raise ValueError(f"Tool {name} not found")
    except Exception as e:
        return [TextContent(type="text", text=f"Execution Error: {str(e)}")]


# --- HTTP / SSE Web Server Setup ---
sse = SseServerTransport("/messages")


async def app(scope, receive, send):
    """
    Pure ASGI application.
    Bypasses all Starlette/FastMCP framework bugs by wiring Uvicorn directly to the MCP SDK.
    """
    if scope["type"] != "http":
        return

    path = scope["path"]
    method = scope["method"]

    if path == "/sse" and method == "GET":
        # Let the MCP SDK handle the SSE stream completely natively
        async with sse.connect_sse(scope, receive, send) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )

    elif path == "/messages" and method == "POST":
        # Let the MCP SDK handle the POST message natively (prevents the Starlette NoneType crash)
        await sse.handle_post_message(scope, receive, send)

    else:
        # Fallback for invalid routes
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"Not Found"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000)
