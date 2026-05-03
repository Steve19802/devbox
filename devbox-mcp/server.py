import subprocess
import socket
import uvicorn
from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp.server.sse import SseServerTransport


def get_running_containers() -> list[str]:
    """Dynamically fetch only sibling containers in this specific Docker Compose project."""
    try:
        # 1. Docker automatically sets the container's hostname to its short container ID
        container_id = socket.gethostname()

        # 2. Inspect this MCP container to find its Docker Compose project name
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

        # If it's not running in Compose, fallback safely
        if not project_name:
            return ["error_no_compose_project_found"]

        # 3. Filter docker ps to ONLY show containers from this exact project stack
        ps_cmd = [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
            "--format",
            "{{.Names}}",
        ]
        ps_result = subprocess.run(ps_cmd, capture_output=True, text=True, check=True)
        names = [name.strip() for name in ps_result.stdout.splitlines() if name.strip()]

        # 4. Find the actual name of THIS container so we can remove it from the AI's list
        my_name_cmd = ["docker", "inspect", "-f", "{{.Name}}", container_id]
        my_name_result = subprocess.run(my_name_cmd, capture_output=True, text=True)
        my_name = my_name_result.stdout.strip().lstrip(
            "/"
        )  # Docker names start with a forward slash

        if my_name in names:
            names.remove(my_name)

        return names if names else ["no_sibling_containers_found"]
    except Exception as e:
        return [f"error_fetching_containers: {str(e)}"]


# Initialize the manual Server
mcp_server = Server("devbox-supervisor")


@mcp_server.list_tools()
async def handle_list_tools() -> list[Tool]:
    dynamic_containers = get_running_containers()

    return [
        Tool(
            name="execute_container_command",
            description="Execute a bash/shell command inside a specific Docker container.",
            inputSchema={
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "enum": dynamic_containers,
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
            container = arguments["container"]
            command = arguments["command"]
            # Validate against the live list of containers
            allowed_containers = get_running_containers()
            if container not in allowed_containers:
                return [
                    TextContent(
                        type="text",
                        text=f"Security Violation: Container {container} not allowed.",
                    )
                ]
            result = subprocess.run(
                ["docker", "exec", container, "sh", "-c", command],
                capture_output=True,
                text=True,
            )
            return [
                TextContent(
                    type="text",
                    text=f"Exit Code: {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
                )
            ]
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
