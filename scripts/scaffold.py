import json
import os
import sys


def load_manifest(build_dir):
    path = os.path.join(build_dir, "templates", "manifest.json")
    with open(path) as f:
        return json.load(f)


def get_available_runtimes(manifest):
    return sorted(manifest["runtimes"].keys())


def interactive_wizard(manifest):
    runtimes = get_available_runtimes(manifest)
    services = []

    while True:
        try:
            count = int(input("How many services will your project need? "))
            if count > 0:
                break
            print("Enter a positive number.")
        except ValueError:
            print("Enter a valid number.")

    for i in range(1, count + 1):
        print(f"\n--- Service #{i} ---")
        name = input("  Name: ").strip()
        while not name:
            name = input("  Name (required): ").strip()

        print(f"  Runtime ({'/'.join(runtimes)}): ", end="")
        runtime = input().strip()
        while runtime not in runtimes:
            print(f"  Available: {', '.join(runtimes)}")
            runtime = input(f"  Runtime: ").strip()

        default_port = manifest["runtimes"][runtime]["default_port"]
        port_input = input(f"  Port [{default_port}]: ").strip()
        port = int(port_input) if port_input else default_port

        services.append({"name": name, "runtime": runtime, "port": port})

    inject_input = input(
        "\nInject Containerized IDE (Neovim/Lazygit)? [y/N]: "
    ).strip().lower()
    inject_ide = inject_input in ("y", "yes")

    return services, inject_ide


def generate_compose(services, output_dir):
    buf = ["services:"]
    for svc in services:
        buf.append(f"  {svc['name']}:")
        buf.append(f"    build:")
        buf.append(f"      context: ./src/{svc['name']}")
        buf.append(f"    ports:")
        buf.append(f'      - "{svc["port"]}:{svc["port"]}"')
        buf.append("")
    content = "\n".join(buf).rstrip() + "\n"

    path = os.path.join(output_dir, "docker-compose.yml")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(content)
        print(f"  Created {path}")
    else:
        print(f"  Skipped {path} (already exists)")


def generate_compose_dev(services, inject_ide, manifest, output_dir):
    buf = ["services:"]
    for svc in services:
        rt = manifest["runtimes"][svc["runtime"]]
        user_dir = rt["home_dir"]

        buf.append(f"  {svc['name']}:")
        buf.append(f"    build:")
        buf.append(f"      target: dev")
        buf.append(f"      args:")
        buf.append(f"        - NEOVIM_VERSION=${{NEOVIM_VERSION:-stable}}")
        buf.append(f"        - LAZYGIT_VERSION=${{LAZYGIT_VERSION:-stable}}")
        buf.append(f'    user: "${{UID}}:${{GID}}"')
        buf.append(f"    volumes:")
        buf.append(f"      - ./src/{svc['name']}:/app")
        if inject_ide:
            buf.append(f"      - ~/.config/nvim:{user_dir}/.config/nvim")
            buf.append(
                f"      - ./src/{svc['name']}/.nvim/share/nvim:{user_dir}/.local/share/nvim"
            )
            buf.append(
                f"      - ./src/{svc['name']}/.nvim/state/nvim:{user_dir}/.local/state/nvim"
            )
        buf.append(f"      - ./.opencode/skills:/var/devbox/skills:ro")
        buf.append(f"    environment:")
        buf.append(f"      - HOME={user_dir}")
        buf.append(f"      - DEVBOX_SERVICE={svc['name']}")
        buf.append("")
    content = "\n".join(buf).rstrip() + "\n"

    path = os.path.join(output_dir, "docker-compose.dev.yml")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(content)
        print(f"  Created {path}")
    else:
        print(f"  Skipped {path} (already exists)")


def generate_dockerfile(svc, manifest, build_dir, inject_ide, output_dir):
    rt = manifest["runtimes"][svc["runtime"]]
    dockerfile_name = rt["dockerfile"]
    src_file = os.path.join(build_dir, "templates", "dockerfiles", dockerfile_name)
    dst_dir = os.path.join(output_dir, "src", svc["name"])
    os.makedirs(dst_dir, exist_ok=True)
    dst_file = os.path.join(dst_dir, "Dockerfile")

    if os.path.exists(dst_file):
        print(f"  Skipped {dst_file} (already exists)")
        return

    with open(src_file) as f:
        content = f.read()

    marker = "@DEVBOX_IDE_INJECTION_POINT@"

    if inject_ide:
        if f"# {marker}" in content:
            ide_path = os.path.join(build_dir, "templates", "ide_injection.dockerfile")
            if os.path.exists(ide_path):
                with open(ide_path) as f:
                    ide_snippet = f.read()
                content = content.replace(f"# {marker}", ide_snippet, 1)
        else:
            print(f"  Note: {svc['name']} Dockerfile has no IDE injection marker. IDE tools are pre-installed or not needed. Skipping.")
    else:
        content = "\n".join(
            line for line in content.split("\n") if marker not in line
        )

    with open(dst_file, "w") as f:
        f.write(content)

    if svc["runtime"] == "python":
        req_file = os.path.join(dst_dir, "requirements.txt")
        if not os.path.exists(req_file):
            with open(req_file, "w") as f:
                f.write("")
            print(f"  Created {req_file}")

    print(f"  Created {dst_file}")


def create_nvim_dirs(services, output_dir):
    for svc in services:
        base = os.path.join(output_dir, "src", svc["name"], ".nvim")
        os.makedirs(os.path.join(base, "share", "nvim"), exist_ok=True)
        os.makedirs(os.path.join(base, "state", "nvim"), exist_ok=True)


def generate_env(output_dir, uid, gid):
    path = os.path.join(output_dir, ".env")
    if os.path.exists(path):
        print(f"  Skipped {path} (already exists)")
        return
    content = f"""UID={uid}
GID={gid}

# Can be 'stable', 'nightly', or a specific tag like 'v0.10.0'
# It is recommended to match with the host version
NEOVIM_VERSION=v0.11.6
LAZYGIT_VERSION=0.60.0
"""
    with open(path, "w") as f:
        f.write(content)
    print(f"  Created {path}")


def generate_gitignore(output_dir):
    path = os.path.join(output_dir, ".gitignore")
    if os.path.exists(path):
        print(f"  Skipped {path} (already exists)")
        return
    content = """.opencode/
.cursor/
.windsurf/
node_modules/
__pycache__/
*.env
"""
    with open(path, "w") as f:
        f.write(content)
    print(f"  Created {path}")


def generate_architecture(services, output_dir):
    path = os.path.join(output_dir, "ARCHITECTURE.md")
    if os.path.exists(path):
        print(f"  Skipped {path} (already exists)")
        return

    service_lines = "\n".join(
        f"    │   ├── {svc['name']}/             <-- Service: {svc['name']}"
        for svc in services
    )

    run_examples = "\n".join(
        f"* `devbox-run {svc['name']} <command>`" for svc in services
    )

    content = f"""# Project Architecture

Welcome to the project! This repository uses the **DevBox Platform** for local development.

## Directory Structure

    .
    ├── ai/                       <-- Project-specific AI overrides (agents/MCPs)
    ├── build/                    <-- Immutable DevBox Infrastructure (Git Submodule/Template)
    ├── src/                      <-- Application Code
{service_lines}
    │
    ├── docker-compose.yml        <-- Production Base (Pure app services, no dev tools)
    └── docker-compose.dev.yml    <-- Local Dev Overrides (Volume mounts, hot-reloading)

## How to Develop Locally

This project runs inside isolated Docker containers. Do not install runtimes or databases on your host machine.

1. **Load the CLI Tools:** `source build/envsetup.sh`
2. **Setup AI Tooling (Optional):** `devbox-ai-setup`
3. **Start the Environment:** `devbox-up`

### Working with the Code

All code inside `src/` is volume-mounted into the containers. Changes you make will instantly hot-reload.

If you need to run a command inside a container, use the wrapper script:
{run_examples}

### The 3-Layer Docker System

We use a 3-tier architecture to maintain production parity:
1. `docker-compose.yml` defines the core production services.
2. `docker-compose.dev.yml` injects development tools and volume mounts.
3. `build/docker-compose.mcp.yml` silently injects AI capabilities in the background.

Please edit `docker-compose.dev.yml` for local testing, and only modify `docker-compose.yml` when adding new production services.
"""
    with open(path, "w") as f:
        f.write(content)
    print(f"  Created {path}")


def main():
    build_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    manifest = load_manifest(build_dir)

    uid = os.environ.get("LOCAL_UID", os.environ.get("UID", "1000"))
    gid = os.environ.get("LOCAL_GID", os.environ.get("GID", "1000"))

    args_list = sys.argv[1:]

    # Determine mode: CLI (--service flags) vs interactive
    service_flag_indices = [i for i, a in enumerate(args_list) if a == "--service"]

    if service_flag_indices:
        project_root = "."
        services = []
        inject_ide = False

        i = 0
        while i < len(args_list):
            if args_list[i] == "--service":
                i += 1
                if i >= len(args_list):
                    print("--service requires a value (name:runtime:port)")
                    sys.exit(1)
                parts = args_list[i].split(":")
                if len(parts) < 2:
                    print(f"Invalid --service '{args_list[i]}'. Expected name:runtime:port")
                    sys.exit(1)
                name = parts[0]
                runtime = parts[1]
                if runtime not in manifest["runtimes"]:
                    print(
                        f"Unknown runtime '{runtime}'. Available: {', '.join(get_available_runtimes(manifest))}"
                    )
                    sys.exit(1)
                port = (
                    int(parts[2])
                    if len(parts) > 2 and parts[2]
                    else manifest["runtimes"][runtime]["default_port"]
                )
                services.append({"name": name, "runtime": runtime, "port": port})
            elif args_list[i] == "--inject-ide":
                inject_ide = True
            elif args_list[i] == "--project-root":
                i += 1
                if i < len(args_list):
                    project_root = args_list[i]
            else:
                # First positional (non-flag) arg is the project root
                if not args_list[i].startswith("--") and ":" not in args_list[i]:
                    project_root = args_list[i]
            i += 1
    else:
        project_root = args_list[0] if args_list else "."
        services, inject_ide = interactive_wizard(manifest)

    project_root = os.path.abspath(project_root)

    print("\nScaffolding project...")

    os.makedirs(os.path.join(project_root, "src"), exist_ok=True)
    os.makedirs(os.path.join(project_root, "ai"), exist_ok=True)

    for svc in services:
        os.makedirs(os.path.join(project_root, "src", svc["name"]), exist_ok=True)

    generate_compose(services, project_root)
    generate_compose_dev(services, inject_ide, manifest, project_root)

    for svc in services:
        generate_dockerfile(svc, manifest, build_dir, inject_ide, project_root)

    if inject_ide:
        create_nvim_dirs(services, project_root)

    generate_env(project_root, uid, gid)
    generate_gitignore(project_root)
    generate_architecture(services, project_root)

    print("\nProject scaffolded successfully!")
    print("Run 'devbox-ai-setup' to configure your AI toolchain.")


if __name__ == "__main__":
    main()
